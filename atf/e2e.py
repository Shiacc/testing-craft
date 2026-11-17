"""端到端流程编排：一条命令跑完「重置环境 → 起服务 → 分层执行 → 归因 → 缺陷 → 报告」。

为什么把它做成"内核 + 薄包装"而不是一个长 Shell 脚本：
- Shell 脚本在 Windows/Linux/CI 三处行为不一致，且错误处理靠 `&&` 堆叠；
- 步骤顺序、超时、就绪探测、清理，本质是**状态机**，用 Python 表达才可测；
- PowerShell 脚本只做参数转发与中文提示，逻辑只有一份（本文件），换平台不用重写。

四条硬约束（缺一条"一次性跑通"就不成立）：
1. **可重复**：默认删除 sqlite 与种子状态，从零开始；
2. **可等待**：起服务后轮询就绪，不用 `Start-Sleep` 猜；
3. **可验收**：退出码即结论，产物落在 `<artifacts>/<run_id>/`；
4. **可对比**：`--repeat N` 输出每次运行签名，连跑 N 次必须完全一致。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import yaml

from . import bugflow, report
from .catalog import ROOT, Catalog, mark_result
from .runner import Outcome, RunResult, parse_junit

CONFIG_PATH = ROOT / "atf" / "config" / "e2e.yaml"


# ---------------------------------------------------------------- 配置


@dataclass
class E2EConfig:
    raw: dict
    path: Path

    @classmethod
    def load(cls, path: Path | None = None) -> "E2EConfig":
        target = path or CONFIG_PATH
        if not target.exists():
            raise FileNotFoundError(f"缺少流程配置：{target}")
        return cls(raw=yaml.safe_load(target.read_text(encoding="utf-8")) or {}, path=target)

    # -- 常用访问器（带默认值，避免到处 .get 链）
    @property
    def project(self) -> str:
        return str(self.raw.get("project", "unknown"))

    @property
    def server(self) -> dict:
        return dict(self.raw.get("server") or {})

    @property
    def setup(self) -> dict:
        return dict(self.raw.get("setup") or {})

    @property
    def suites(self) -> list[dict]:
        return list(self.raw.get("suites") or [])

    @property
    def filing(self) -> dict:
        return dict(self.raw.get("filing") or {})

    @property
    def timeouts(self) -> dict:
        return dict(self.raw.get("timeouts") or {})

    @property
    def artifacts_dir(self) -> Path:
        return ROOT / str((self.raw.get("artifacts") or {}).get("dir", "atf/e2e-runs"))

    def resolve(self, rel: str) -> Path:
        return (ROOT / rel).resolve()


# ---------------------------------------------------------------- 工具


def _python() -> str:
    return sys.executable


def _expand(args: list[str], *, host: str = "", port: int = 0) -> list[str]:
    return [str(a).replace("{python}", _python()).replace("{host}", host).replace("{port}", str(port)) for a in args]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, port)) == 0


def _wait_port(host: str, port: int, timeout_s: float, interval_s: float = 0.2) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            if sock.connect_ex((host, port)) == 0:
                return True
        time.sleep(interval_s)
    return False


def _wait_http(url: str, timeout_s: float, interval_s: float = 1.0) -> tuple[bool, str]:
    deadline = time.time() + timeout_s
    last = ""
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:  # noqa: S310 - 仅本机回环
                if resp.status < 400:
                    return True, f"HTTP {resp.status}"
                last = f"HTTP {resp.status}"
        except urllib.error.HTTPError as exc:  # 4xx 也算"服务在响应"，但就绪判定要求 <400
            last = f"HTTP {exc.code}"
        except Exception as exc:  # noqa: BLE001
            last = f"{type(exc).__name__}"
        time.sleep(interval_s)
    return False, last or "无响应"


# ---------------------------------------------------------------- 服务生命周期


class E2EEnvError(RuntimeError):
    """环境/凭据类问题：退出码 3，提示先修环境，而不是测试失败。"""


class ServerHandle:
    def __init__(self, cfg: E2EConfig, run_dir: Path, port: int) -> None:
        self.cfg = cfg
        self.run_dir = run_dir
        self.port = port
        self.proc: subprocess.Popen | None = None
        self.log_path = run_dir / str(cfg.server.get("log_file", "server.log"))
        self.base_url = f"http://{cfg.server.get('host', '127.0.0.1')}:{port}"

    def start(self) -> None:
        cwd = self.cfg.resolve(str(self.cfg.server.get("cwd", ".")))
        command = _expand(list(self.cfg.server.get("command") or []), host=str(self.cfg.server.get("host", "127.0.0.1")), port=self.port)
        if not command:
            raise RuntimeError("server.command 未配置")
        self.run_dir.mkdir(parents=True, exist_ok=True)
        log = self.log_path.open("w", encoding="utf-8", errors="replace")
        env = dict(os.environ, PYTHONUTF8="1", PYTHONUNBUFFERED="1")
        self.proc = subprocess.Popen(  # noqa: S603 - 命令来自仓库内配置
            command,
            cwd=cwd,
            stdout=log,
            stderr=subprocess.STDOUT,
            env=env,
        )

    def wait_ready(self) -> None:
        host = str(self.cfg.server.get("host", "127.0.0.1"))
        if _port_in_use(host, self.port) is False and self.proc and self.proc.poll() is not None:
            raise RuntimeError(f"服务启动即退出（exit {self.proc.returncode}），见 {self.log_path.name}")
        ready, detail = _wait_http(
            self.base_url + str(self.cfg.server.get("ready_path", "/")),
            float(self.cfg.server.get("ready_timeout_s", 90)),
            float(self.cfg.server.get("ready_interval_s", 1.0)),
        )
        if not ready:
            raise RuntimeError(f"服务在超时内未就绪（最后状态：{detail}），见 {self.log_path.name}")

    def stop(self) -> None:
        if not self.proc or self.proc.poll() is not None:
            return
        self.proc.terminate()
        try:
            self.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=10)


class MockTrackers:
    """本地 mock 禅道 + Jira：让"提交缺陷"这一步真的被走通，而不是只打印。"""

    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir
        self.port = _free_port()
        self.proc: subprocess.Popen | None = None
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.log_path = run_dir / "mock-tracker.log"

    def __enter__(self) -> "MockTrackers":
        log = self.log_path.open("w", encoding="utf-8", errors="replace")
        self.proc = subprocess.Popen(  # noqa: S603
            [_python(), "-m", "atf.bugfiler.mock_server", "--port", str(self.port), "--token", "mock-token"],
            cwd=ROOT,
            stdout=log,
            stderr=subprocess.STDOUT,
            env=dict(os.environ, PYTHONUTF8="1", PYTHONUNBUFFERED="1"),
        )
        if not _wait_port("127.0.0.1", self.port, 20.0):
            raise RuntimeError(f"mock 缺陷服务未就绪（端口 {self.port} 无响应），见 {self.log_path.name}")
        return self

    def apply_env(self) -> None:
        os.environ.update(
            {
                "ZENTAO_BASE_URL": self.base_url,
                "ZENTAO_PRODUCT_ID": "1",
                "ZENTAO_TOKEN": "mock-token",
                "JIRA_BASE_URL": self.base_url,
                "JIRA_EMAIL": "qa@example.com",
                "JIRA_API_TOKEN": "mock-token",
                "JIRA_PROJECT_KEY": "QA",
            }
        )

    def __exit__(self, *exc) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()


# ---------------------------------------------------------------- 单次运行


@dataclass
class SuiteOutcome:
    id: str
    name: str
    marker: str
    status: str  # PASS / FAIL / EXPECTED_FAIL / FIXED / ERROR
    counts: dict[str, int] = field(default_factory=dict)
    outcomes: list[Outcome] = field(default_factory=list)
    duration_s: float = 0.0
    junit: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "marker": self.marker,
            "status": self.status,
            "counts": self.counts,
            "duration_s": round(self.duration_s, 2),
            "junit": self.junit,
            "failures": [
                {"case_id": o.case_id, "nodeid": o.nodeid, "message": o.message[:300]}
                for o in self.outcomes
                if o.status in ("fail", "error")
            ],
        }


@dataclass
class RunSummary:
    run_id: str
    started_at: str
    finished_at: str = ""
    duration_s: float = 0.0
    suites: list[SuiteOutcome] = field(default_factory=list)
    filing: list = field(default_factory=list)
    coverage: dict = field(default_factory=dict)
    ok: bool = False
    exit_code: int = 0
    error: str = ""
    run_dir: str = ""

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_s": round(self.duration_s, 1),
            "ok": self.ok,
            "error": self.error,
            "suites": [s.to_dict() for s in self.suites],
            "filing": [item.__dict__ for item in self.filing],
            "coverage": self.coverage,
            "run_dir": self.run_dir,
        }

    def signature(self) -> dict:
        """用于 `--repeat` 一致性对比：只保留"应该稳定"的部分。"""
        return {
            "suites": {
                s.id: {"status": s.status, "counts": dict(sorted(s.counts.items()))} for s in self.suites
            },
            "coverage": {
                "rate": self.coverage.get("overall", {}).get("rate"),
                "automated": self.coverage.get("overall", {}).get("automated"),
                "total": self.coverage.get("overall", {}).get("total"),
            },
            "filed": sorted(f"{f.case_id}:{f.action}" for f in self.filing if getattr(f, "action", "") in ("filed", "would-file")),
        }


def _reset_environment(cfg: E2EConfig, run_dir: Path, disable: bool) -> None:
    log = (run_dir / "setup.log").open("w", encoding="utf-8", errors="replace")
    try:
        for rel in cfg.setup.get("reset_files") or []:
            path = cfg.resolve(str(rel))
            if disable:
                log.write(f"[skip] 保留 {rel}\n")
                continue
            if path.exists():
                path.unlink()
                log.write(f"[reset] 删除 {rel}\n")
            else:
                log.write(f"[reset] 不存在（跳过）{rel}\n")

        cwd = cfg.resolve(str(cfg.server.get("cwd", ".")))
        for command in cfg.setup.get("commands") or []:
            cmd = _expand(list(command))
            log.write(f"\n$ {' '.join(cmd)} (cwd={cwd})\n")
            log.flush()
            proc = subprocess.run(
                cmd,
                cwd=cwd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=dict(os.environ, PYTHONUTF8="1"),
                timeout=float(cfg.timeouts.get("setup_s", 300)),
            )
            log.write(proc.stdout or "")
            log.write(proc.stderr or "")
            if proc.returncode != 0:
                raise RuntimeError(f"准备命令失败：{' '.join(cmd)}（exit {proc.returncode}），见 setup.log")
    finally:
        log.close()


def _run_suite(cfg: E2EConfig, suite: dict, run_dir: Path, catalog: Catalog) -> SuiteOutcome:
    suite_id = str(suite["id"])
    marker = str(suite.get("marker", ""))
    junit_path = run_dir / f"junit-{suite_id}.xml"
    stdout_path = run_dir / f"console-{suite_id}.log"

    env = dict(os.environ, PYTHONUTF8="1")
    env.update({str(k): str(v) for k, v in (suite.get("env") or {}).items()})

    cmd = [_python(), "-X", "utf8", "-m", "pytest", "-q", f"--junitxml={junit_path}"]
    if marker:
        cmd += ["-m", marker]

    started = time.perf_counter()
    proc = subprocess.run(
        cmd,
        cwd=cfg.resolve(str(cfg.raw.get("suites_cwd", "labs/07-real-project"))),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=float(cfg.timeouts.get("suite_s", 900)),
    )
    duration = time.perf_counter() - started
    stdout_path.write_text((proc.stdout or "") + "\n--- stderr ---\n" + (proc.stderr or ""), encoding="utf-8", newline="\n")

    outcomes = parse_junit(junit_path)
    for outcome in outcomes:
        case = catalog.by_test(outcome.nodeid)
        if case is None:
            continue
        outcome.case_id = case.id
        mark_result(case, outcome.status)
        if outcome.status in ("fail", "error"):
            case.last_result = outcome.status

    counts = {"pass": 0, "fail": 0, "error": 0, "skip": 0}
    for outcome in outcomes:
        counts[outcome.status] = counts.get(outcome.status, 0) + 1
    failures = [o for o in outcomes if o.status in ("fail", "error")]
    expect_failures = bool(suite.get("expect_failures"))

    if not outcomes:
        status = "ERROR"
    elif expect_failures and failures:
        status = "EXPECTED_FAIL"
    elif expect_failures and not failures:
        status = "FIXED"
    elif failures:
        status = "FAIL"
    else:
        status = "PASS"

    return SuiteOutcome(
        id=suite_id,
        name=str(suite.get("name", suite_id)),
        marker=marker,
        status=status,
        counts=counts,
        outcomes=outcomes,
        duration_s=duration,
        junit=str(junit_path.relative_to(run_dir).as_posix()),
    )


def _filing(cfg: E2EConfig, summary: RunSummary, catalog: Catalog, mode: str, tracker: str, run_dir: Path) -> list:
    failures = [o for s in summary.suites for o in s.outcomes if o.status in ("fail", "error")]
    if not failures:
        return []

    shim = RunResult(run_id=summary.run_id, started_at=summary.started_at, outcomes=failures)
    trackers = ["zentao", "jira"] if tracker == "both" else [tracker]
    collected: list = []

    if mode == "dry-run":
        for name in trackers:
            collected.append(("dry-run", name, bugflow.file_failures(shim, catalog, tracker=name, dry_run=True, base_url="见配置")))
        return [item for _, _, group in collected for item in group]

    if mode == "mock":
        with MockTrackers(run_dir) as mock:
            mock.apply_env()
            for name in trackers:
                state = run_dir / f"state-{name}.json" if (cfg.filing.get("isolate_state", True)) else None
                collected.append(
                    (
                        "mock",
                        name,
                        bugflow.file_failures(shim, catalog, tracker=name, dry_run=False, state_path=state, base_url=mock.base_url),
                    )
                )
        return [item for _, _, group in collected for item in group]

    if mode == "submit":
        for name in trackers:
            ready, why = bugflow.tracker_ready(name)
            if not ready:
                raise E2EEnvError(f"submit 模式缺少 {name} 凭据：{why}")
        for name in trackers:
            collected.append(("submit", name, bugflow.file_failures(shim, catalog, tracker=name, dry_run=False, base_url="")))
        return [item for _, _, group in collected for item in group]

    raise ValueError(f"未知 filing.mode：{mode}")


def run_once(cfg: E2EConfig, *, mode: str, tracker: str, port: int | None = None, keep_server: bool = False, reset: bool = True) -> RunSummary:
    catalog = Catalog.load()
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = cfg.artifacts_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    summary = RunSummary(run_id=run_id, started_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"), run_dir=str(run_dir.relative_to(ROOT).as_posix()))
    started = time.perf_counter()
    server: ServerHandle | None = None

    try:
        _reset_environment(cfg, run_dir, disable=not reset)

        chosen_port = port or int(cfg.server.get("port", 8099))
        host = str(cfg.server.get("host", "127.0.0.1"))
        if _port_in_use(host, chosen_port):
            raise E2EEnvError(
                f"端口 {chosen_port} 已被占用。先停掉占用进程，或用 --port 指定其他端口（本次未做自动改端口，避免与旧服务混淆）"
            )

        server = ServerHandle(cfg, run_dir, chosen_port)
        server.start()
        server.wait_ready()

        for suite in cfg.suites:
            outcome = _run_suite(cfg, suite, run_dir, catalog)
            summary.suites.append(outcome)
            print(
                f"  [{outcome.status:<13}] {outcome.id:<7} pass {outcome.counts.get('pass', 0):>2} "
                f"fail {outcome.counts.get('fail', 0) + outcome.counts.get('error', 0):>2}  {outcome.duration_s:>5.1f}s"
            )

        catalog.save()
        summary.coverage = catalog.coverage()
        summary.filing = _filing(cfg, summary, catalog, mode=mode, tracker=tracker, run_dir=run_dir)

        for item in summary.filing:
            print(f"  [{item.action}] {item.case_id or '-'} {item.tracker or ''} {item.key or ''}".rstrip())

        required = [s for s in summary.suites if next((x for x in cfg.suites if str(x["id"]) == s.id), {}).get("required", False)]
        summary.ok = bool(required) and all(s.status == "PASS" for s in required)
        if not summary.ok and summary.exit_code == 0:
            summary.exit_code = 1
        fixed = [s.id for s in summary.suites if s.status == "FIXED"]
        if fixed:
            summary.error = f"演练缺陷已被修复（{', '.join(fixed)}）：请把 canary 用例转正并更新基线"
    except E2EEnvError as exc:
        summary.ok = False
        summary.exit_code = 3
        summary.error = f"环境未就绪：{exc}"
    except Exception as exc:  # noqa: BLE001
        summary.ok = False
        summary.exit_code = 3
        summary.error = f"{type(exc).__name__}: {exc}"
    finally:
        if server and not keep_server:
            server.stop()
        else:
            if server:
                print(f"  [keep] 服务仍在运行：{server.base_url}")
        summary.finished_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        summary.duration_s = time.perf_counter() - started

    _write_artifacts(cfg, summary, run_dir)
    return summary


def _write_artifacts(cfg: E2EConfig, summary: RunSummary, run_dir: Path) -> None:
    (run_dir / "results.json").write_text(
        json.dumps(summary.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8", newline="\n"
    )

    lines = [
        f"# 端到端运行报告 · {summary.run_id}",
        "",
        f"- 项目：{cfg.project}",
        f"- 时间：{summary.started_at} → {summary.finished_at}（{summary.duration_s:.1f}s）",
        f"- 结论：{'通过' if summary.ok else '未通过'}",
    ]
    if summary.error:
        lines.append(f"- 原因：{summary.error}")
    lines += ["", "## 分层结果", "", "| 套件 | marker | 状态 | pass | fail | skip | 耗时 |", "|---|---|---|---|---|---|---|"]
    for suite in summary.suites:
        lines.append(
            f"| {suite.id} | `{suite.marker}` | {suite.status} | {suite.counts.get('pass', 0)} | "
            f"{suite.counts.get('fail', 0) + suite.counts.get('error', 0)} | {suite.counts.get('skip', 0)} | {suite.duration_s:.1f}s |"
        )

    failures = [o for s in summary.suites for o in s.outcomes if o.status in ("fail", "error")]
    if failures:
        lines += ["", "## 失败明细", "", "| 用例 | 测试 | 状态 | 摘要 |", "|---|---|---|---|"]
        for outcome in failures:
            first = (outcome.message or "").splitlines()
            lines.append(
                f"| {outcome.case_id or '（黑户）'} | `{outcome.nodeid}` | {outcome.status} | "
                f"{(first[0][:80] if first else '')} |"
            )

    if summary.filing:
        lines += ["", "## 缺陷流", "", "| 用例 | 动作 | 系统 | 单号 | 说明 |", "|---|---|---|---|---|"]
        for item in summary.filing:
            lines.append(
                f"| {item.case_id or '-'} | {item.action} | {item.tracker or '-'} | {item.key or '-'} | {item.detail[:70]} |"
            )

    coverage = summary.coverage or {}
    overall = coverage.get("overall") or {}
    lines += [
        "",
        "## 覆盖率",
        "",
        f"- 总自动化率：{overall.get('rate', 0):.0%}（{overall.get('automated', 0)}/{overall.get('total', 0)}）",
    ]
    for layer, stat in (coverage.get("by_layer") or {}).items():
        if stat.get("total"):
            lines.append(f"  - {layer}: {stat['automated']}/{stat['total']} = {stat['rate']:.0%}")

    lines += [
        "",
        "## 产物",
        "",
        f"- 配置：`{cfg.path.relative_to(ROOT).as_posix()}`",
        f"- JUnit：`{run_dir.relative_to(ROOT).as_posix()}/junit-*.xml`",
        "- 控制台：`console-<suite>.log`、`setup.log`、`server.log`",
        "",
        "## 复现",
        "",
        "```bash",
        f"python -m atf e2e --filing {cfg.filing.get('mode', 'mock')} --tracker {cfg.filing.get('tracker', 'both')}",
        "```",
    ]
    (run_dir / "RUN.md").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


# ---------------------------------------------------------------- CLI


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="atf e2e", description="端到端：重置 → 起服务 → 分层执行 → 归因 → 缺陷 → 报告")
    parser.add_argument("--config", default=None, help="流程配置路径（默认 atf/config/e2e.yaml）")
    parser.add_argument("--filing", default=None, choices=["dry-run", "mock", "submit"], help="缺陷联动方式")
    parser.add_argument("--tracker", default=None, choices=["zentao", "jira", "both"], help="缺陷系统")
    parser.add_argument("--port", type=int, default=None, help="覆盖被测服务端口")
    parser.add_argument("--no-reset", action="store_true", help="不删除 sqlite 与种子状态（调试用）")
    parser.add_argument("--keep-server", action="store_true", help="结束后保留服务（便于手工排查）")
    parser.add_argument("--repeat", type=int, default=1, help="连跑 N 次并对比结果一致性（验收用）")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = E2EConfig.load(Path(args.config) if args.config else None)
    except Exception as exc:  # noqa: BLE001
        print(f"[配置错误] {exc}")
        return 2

    mode = args.filing or str(cfg.filing.get("mode", "dry-run"))
    tracker = args.tracker or str(cfg.filing.get("tracker", "zentao"))

    signatures: list[dict] = []
    summaries: list[RunSummary] = []
    for index in range(1, max(1, args.repeat) + 1):
        if args.repeat > 1:
            print(f"\n===== 第 {index}/{args.repeat} 次运行 =====")
        print(f"项目 {cfg.project}｜缺陷联动 {mode}/{tracker}｜重置 {'否' if args.no_reset else '是'}")
        summary = run_once(
            cfg,
            mode=mode,
            tracker=tracker,
            port=args.port,
            keep_server=args.keep_server,
            reset=not args.no_reset,
        )
        summaries.append(summary)
        signatures.append(summary.signature())
        print(f"结论：{'通过' if summary.ok else '未通过'}（退出码 {summary.exit_code}）｜{summary.duration_s:.1f}s｜{summary.run_dir}")
        if summary.error:
            print(f"  原因：{summary.error}")

    if args.repeat > 1:
        base = signatures[0]
        consistent = all(sig == base for sig in signatures[1:])
        print("\n===== 一致性对比 =====")
        for index, sig in enumerate(signatures, start=1):
            same = "一致" if sig == base else "不一致"
            brief = " ".join(f"{key}={value['status']}" for key, value in sig["suites"].items())
            print(f"  第 {index} 次：{brief} → 与第 1 次{same}")
        if not consistent:
            print("  [FAIL] 多次运行结果不一致，说明存在未消除的外部依赖或数据残留")
        if not consistent:
            return 1
        return max((s.exit_code for s in summaries), default=0)

    return summaries[0].exit_code if summaries else 2
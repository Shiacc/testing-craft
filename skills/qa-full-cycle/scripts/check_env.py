#!/usr/bin/env python3
"""qa-full-cycle 环境体检。

检查：Python 版本、pytest、httpx、atf 是否可用，被测项目是否在线，当前目录是否可写。
只用标准库 + subprocess，无第三方依赖，可独立运行。

用法：
    python skills/qa-full-cycle/scripts/check_env.py
    python skills/qa-full-cycle/scripts/check_env.py --target-url http://localhost:8000
    python skills/qa-full-cycle/scripts/check_env.py --target-url http://localhost:8000 --require-target
    python skills/qa-full-cycle/scripts/check_env.py --skip-network --json

退出码：
    0   体检通过：没有 FAIL 项（WARN 不阻塞，但必须知道为什么可以接受）
    1   存在 FAIL 项：硬性缺失，先修环境再往下走，不许跳过
    64  参数错误

三态口径：
    OK    必需项通过
    WARN  非必需项缺失或有风险，不阻塞流程（如 httpx 未装 → 只是 L3 接口层跑不了）
    FAIL  硬性缺失，阻塞后续步骤

环境变量：
    QA_TARGET_URL   未传 --target-url 时的默认被测地址
"""

from __future__ import annotations

import argparse
import importlib.metadata
import importlib.util
import json
import os
import platform
import subprocess
import sys
import unicodedata
import urllib.error
import urllib.request
from pathlib import Path

MIN_PY = (3, 10)
RECOMMENDED_PY = (3, 12)
DEFAULT_TARGET = "http://127.0.0.1:8000"
SCRIPT_DIR = Path(__file__).resolve().parent


class Parser(argparse.ArgumentParser):
    """参数错误统一用 64（sysexits EX_USAGE），与"体检失败"的 1 区分开。"""

    def error(self, message: str) -> None:  # type: ignore[override]
        self.print_usage(sys.stderr)
        sys.stderr.write(f"参数错误: {message}\n")
        raise SystemExit(64)


def _run(cmd: list[str], timeout: int = 120) -> tuple[int, str]:
    """跑一条子命令，返回 (退出码, 合并后的输出)。找不到命令/超时折成 127/124。"""
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError:
        return 127, "命令不存在"
    except subprocess.TimeoutExpired:
        return 124, f"超时（>{timeout}s）"
    except OSError as exc:  # 权限、路径异常等
        return 126, str(exc)
    return proc.returncode, f"{proc.stdout or ''}{proc.stderr or ''}".strip()


def _pad(text: str, width: int) -> str:
    """按显示宽度补空格（中文按 2 列算），让中文与英文混排的列对齐。"""
    shown = sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)
    return text + " " * max(0, width - shown)


def _first_line(text: str, limit: int = 90) -> str:
    for line in text.splitlines():
        line = line.strip()
        if line:
            return line[:limit]
    return ""


def _module_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None
    except Exception:  # 元数据损坏不应让体检崩掉
        return None


def row(level: str, check: str, detail: str, hint: str = "") -> dict[str, str]:
    return {"level": level, "check": check, "detail": detail, "hint": hint}


def check_python() -> dict[str, str]:
    version = f"{platform.python_version()} ({sys.executable})"
    current = (sys.version_info.major, sys.version_info.minor)
    if current < MIN_PY:
        return row("FAIL", "Python 版本", version, f"要求 ≥{MIN_PY[0]}.{MIN_PY[1]}，请换解释器或建 venv")
    if current < RECOMMENDED_PY:
        return row(
            "WARN",
            "Python 版本",
            version,
            f"本仓库验证于 {RECOMMENDED_PY[0]}.{RECOMMENDED_PY[1]}，低于该版本可能有依赖不兼容",
        )
    return row("OK", "Python 版本", version)


def check_pytest() -> dict[str, str]:
    spec = importlib.util.find_spec("pytest")
    if spec is None:
        return row("FAIL", "pytest", "未安装", "python -m pip install pytest")
    version = _module_version("pytest") or "版本未知"
    code, out = _run([sys.executable, "-m", "pytest", "--version"], timeout=60)
    if code != 0:
        return row("FAIL", "pytest", f"{version}，但 python -m pytest 不可调用", _first_line(out))
    return row("OK", "pytest", f"{version}（python -m pytest 可调用）")


def check_httpx() -> dict[str, str]:
    if importlib.util.find_spec("httpx") is None:
        return row(
            "WARN",
            "httpx",
            "未安装",
            "L3 接口层与缺陷系统调用需要它：python -m pip install httpx",
        )
    return row("OK", "httpx", _module_version("httpx") or "已安装")


def check_atf() -> dict[str, str]:
    code, out = _run([sys.executable, "-m", "atf", "--help"], timeout=120)
    if code == 0:
        return row("OK", "atf", f"可调用（python -m atf）：{_first_line(out)}")
    lowered = out.lower()
    if "no module named" in lowered:
        detail = "未安装（当前解释器找不到 atf 模块）"
    elif code == 124:
        detail = "调用超时：python -m atf --help 无响应"
    else:
        detail = f"不可调用：{_first_line(out) or f'退出码 {code}'}"
    return row(
        "WARN",
        "atf",
        detail,
        "run_cycle.py 需要它（退出码 3）；替代路径见 references/workflow.md S3「失败回退」",
    )


def check_target(url: str, timeout: float, require: bool) -> dict[str, str]:
    request = urllib.request.Request(url, method="GET", headers={"User-Agent": "qa-full-cycle/check_env"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.status
    except urllib.error.HTTPError as exc:  # 有响应就是"在线"，只是业务/鉴权层报错
        status = exc.code
    except (urllib.error.URLError, OSError, ValueError) as exc:
        reason = getattr(exc, "reason", exc)
        level = "FAIL" if require else "WARN"
        return row(
            level,
            "被测项目在线",
            f"{url} 不可达：{reason}",
            "先起服务再跑；或确认 URL/端口；本轮若只做静态识别可忽略（--require-target 会要求在线）",
        )

    if status >= 500:
        return row(
            "WARN",
            "被测项目在线",
            f"{url} → HTTP {status}（服务在，但自身报错）",
            "5xx 会让执行结果不可信，先查服务日志",
        )
    return row("OK", "被测项目在线", f"{url} → HTTP {status}")


def check_workdir() -> dict[str, str]:
    cwd = Path.cwd()
    if not cwd.is_dir():
        return row("FAIL", "执行目录", f"{cwd} 不可访问", "cd 到仓库根目录后重跑")
    probe = cwd / f".qa-full-cycle-probe-{os.getpid()}"
    writable = True
    error = ""
    try:
        probe.write_text("ok", encoding="utf-8")
    except OSError as exc:
        writable, error = False, str(exc)
    finally:
        try:
            probe.unlink(missing_ok=True)
        except OSError:
            pass  # 探针残留不影响结论，也别把"删不掉"误报成"不可写"
    if not writable:
        return row("FAIL", "执行目录", f"{cwd} 不可写：{error}", "执行产物需要落盘，换有写权限的目录")
    return row("OK", "执行目录", f"{cwd}（可写）")


def render(rows: list[dict[str, str]]) -> None:
    width = max(
        sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in item["check"])
        for item in rows
    )
    print("环境体检 · qa-full-cycle")
    print(f"解释器: {sys.executable}")
    print(f"工作目录: {Path.cwd()}")
    print("-" * 72)
    for item in rows:
        print(f"[{item['level']:<4}] {_pad(item['check'], width)}  {item['detail']}")
        if item["hint"] and item["level"] != "OK":
            print(f"{'':6}  {'':<{width}}  → {item['hint']}")
    print("-" * 72)
    fails = sum(1 for item in rows if item["level"] == "FAIL")
    warns = sum(1 for item in rows if item["level"] == "WARN")
    if fails:
        print(f"结论: 未通过（FAIL {fails} 项，WARN {warns} 项） 退出码 1")
        print("下一步: 修掉 FAIL，不要靠跳过体检开始流程（见 references/workflow.md S0「失败回退」）")
    elif warns:
        print(f"结论: 通过，但有风险（WARN {warns} 项） 退出码 0")
        print("下一步: 每条 WARN 都要能说出'为什么可以接受'，否则当作 FAIL 处理")
    else:
        print("结论: 通过 退出码 0")
        print("下一步: 进入 S1 识别项目（references/workflow.md）")


def main(argv: list[str] | None = None) -> int:
    parser = Parser(
        prog="check_env.py",
        description="qa-full-cycle 环境体检：python / pytest / httpx / atf / 被测项目在线 / 执行目录可写",
        epilog=(
            "示例:\n"
            "  python check_env.py\n"
            "  python check_env.py --target-url http://localhost:8000 --require-target\n"
            "  python check_env.py --skip-network --json\n"
            "退出码: 0 通过 / 1 存在 FAIL / 64 参数错误"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--target-url",
        default=os.environ.get("QA_TARGET_URL", DEFAULT_TARGET),
        help=f"被测项目地址（默认取 QA_TARGET_URL，否则 {DEFAULT_TARGET}）",
    )
    parser.add_argument("--timeout", type=float, default=3.0, help="HTTP 探测超时秒数（默认 3）")
    parser.add_argument(
        "--require-target",
        action="store_true",
        help="被测项目不可达时记为 FAIL 而不是 WARN（本轮必须真跑时加上）",
    )
    parser.add_argument("--skip-network", action="store_true", help="跳过在线探测（纯粹查本机依赖时用）")
    parser.add_argument("--json", action="store_true", help="输出 JSON，便于 CI 解析")
    args = parser.parse_args(argv)

    rows = [check_python(), check_pytest(), check_httpx(), check_atf()]
    if args.skip_network:
        rows.append(row("OK", "被测项目在线", "已跳过（--skip-network）"))
    else:
        rows.append(check_target(args.target_url, args.timeout, args.require_target))
    rows.append(check_workdir())

    exit_code = 1 if any(item["level"] == "FAIL" for item in rows) else 0
    if args.json:
        payload = {
            "skill": "qa-full-cycle",
            "python": platform.python_version(),
            "executable": sys.executable,
            "cwd": str(Path.cwd()),
            "exit_code": exit_code,
            "rows": rows,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        render(rows)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
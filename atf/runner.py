"""执行层：跑测试 → 解析 JUnit → 回写用例库 → 产出运行记录。

关键设计：
1. **以 JUnit 为唯一事实来源**（不解析控制台文本），CI 里同一份 XML 还能喂给别的系统；
2. **结果回写用例库**：每条自动化用例的 last_run / last_result 由执行产生，
   这样"用例库是活的"——不是一份三个月没人动的 Excel；
3. **区分 bound（有用例）与 unbound（黑户）**：黑户测试要在报告里显式列出来，
   否则"覆盖率 100%"可能只是"用例库没跟上代码"。
"""

from __future__ import annotations

import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .catalog import LAB_DIR, ROOT, Catalog, _normalize, mark_result

RUNS_DIR = ROOT / "atf" / "runs"


@dataclass
class Outcome:
    nodeid: str
    name: str
    status: str  # pass / fail / error / skip
    duration: float = 0.0
    message: str = ""
    case_id: str = ""


@dataclass
class RunResult:
    run_id: str
    started_at: str
    finished_at: str = ""
    duration_s: float = 0.0
    returncode: int = 0
    outcomes: list[Outcome] = field(default_factory=list)
    unbound: list[str] = field(default_factory=list)
    junit: str = ""
    run_dir: str = ""

    @property
    def failures(self) -> list[Outcome]:
        return [item for item in self.outcomes if item.status in ("fail", "error")]

    def counts(self) -> dict[str, int]:
        summary = {"pass": 0, "fail": 0, "error": 0, "skip": 0}
        for item in self.outcomes:
            summary[item.status] = summary.get(item.status, 0) + 1
        return summary


def _status_of(testcase: ET.Element) -> tuple[str, str]:
    for child in testcase:
        tag = child.tag.lower()
        if tag in ("failure", "error"):
            text = (child.get("message") or child.text or "").strip()
            return ("fail" if tag == "failure" else "error", text[:500])
        if tag == "skipped":
            return ("skip", (child.get("message") or "").strip()[:200])
    return ("pass", "")


def _nodeid_of(testcase: ET.Element) -> tuple[str, str]:
    classname = testcase.get("classname", "") or ""
    name = testcase.get("name", "") or ""
    path = classname.replace(".", "/") + ".py" if classname else "unknown.py"
    return f"{path}::{name}", name


def parse_junit(path: Path) -> list[Outcome]:
    if not path.exists():
        return []
    root = ET.parse(path).getroot()
    containers = [root] if root.tag == "testsuite" else root.findall(".//testsuite")
    outcomes: list[Outcome] = []
    for suite in containers:
        for testcase in suite.findall("testcase"):
            nodeid, name = _nodeid_of(testcase)
            status, message = _status_of(testcase)
            outcomes.append(
                Outcome(
                    nodeid=nodeid,
                    name=name.split("[")[0],
                    status=status,
                    duration=float(testcase.get("time", 0.0) or 0.0),
                    message=message,
                )
            )
    return outcomes


def run(
    tag: str | None = None,
    canary: bool = False,
    extra_args: list[str] | None = None,
    catalog: Catalog | None = None,
    lab_dir: Path | None = None,
    target_url: str | None = None,
) -> RunResult:
    target = lab_dir or LAB_DIR
    if not target.exists():
        raise FileNotFoundError(f"被测实验室目录不存在：{target}")

    cat = catalog or Catalog.load()
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = RUNS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    junit = run_dir / "junit.xml"

    cmd = [sys.executable, "-X", "utf8", "-m", "pytest", "-q", f"--junitxml={junit}"]
    if tag:
        cmd += ["-m", tag]
    if extra_args:
        cmd += extra_args

    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    if target_url:
        env["QA_BASE_URL"] = target_url
    if canary:
        env["QA_RUN_CANARY"] = "1"

    started = datetime.now()
    proc = subprocess.run(
        cmd,
        cwd=target,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=1800,
    )
    finished = datetime.now()

    outcomes = parse_junit(junit)
    stamp = finished.strftime("%Y-%m-%d %H:%M:%S")
    bound_nodeids: set[str] = set()
    for outcome in outcomes:
        case = cat.by_test(outcome.nodeid)
        if case is None:
            continue
        outcome.case_id = case.id
        bound_nodeids.add(_normalize(outcome.nodeid))
        mark_result(case, outcome.status, stamp)
        # 参数化用例：一条用例绑定多个 nodeid，任何一条失败即视为失败
        if outcome.status in ("fail", "error"):
            case.last_result = outcome.status

    cat.save()

    result = RunResult(
        run_id=run_id,
        started_at=started.strftime("%Y-%m-%d %H:%M:%S"),
        finished_at=stamp,
        duration_s=(finished - started).total_seconds(),
        returncode=proc.returncode,
        outcomes=outcomes,
        unbound=sorted({_normalize(item.nodeid) for item in outcomes} - bound_nodeids),
        junit=str(junit.relative_to(ROOT)) if junit.exists() else "",
        run_dir=str(run_dir.relative_to(ROOT)),
    )

    # 保存控制台尾部，便于失败时不用重跑也能看原因
    (run_dir / "console.log").write_text(
        (proc.stdout or "") + "\n--- stderr ---\n" + (proc.stderr or ""), encoding="utf-8", newline="\n"
    )

    return result
"""调度与执行层：把套件切成任务、并发跑到执行机上、把结果结构化收回来。

这里对应 docs/04 §5 的四个组件：调度器（plan）、执行机池（ThreadPool）、
结果存储（run 目录）、门禁引擎（evaluate）。真实平台把这四件事拆成服务，
本实验用单进程实现同样的语义，便于理解每个组件的边界。
"""

from __future__ import annotations

import subprocess
import time
import uuid
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from .registry import Gate, Suite

ISO = "%Y-%m-%d %H:%M:%S"


@dataclass
class Task:
    suite_id: str
    name: str
    shard: int
    total_shards: int
    files: list[str]
    kind: str
    timeout_s: int


@dataclass
class TaskResult:
    suite_id: str
    shard: int
    status: str          # PASS / FAIL / TIMEOUT / ERROR
    tests: int = 0
    failures: int = 0
    errors: int = 0
    skipped: int = 0
    duration_s: float = 0.0
    junit: str | None = None
    message: str = ""
    files: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- 调度（plan）


def discover_test_files(suite: Suite) -> list[str]:
    """返回相对工作目录的路径（如 tests/test_api.py），可直接作为 pytest 参数。"""
    if not suite.shardable:
        return []
    return sorted(
        path.relative_to(suite.cwd).as_posix() for path in (suite.cwd / "tests").glob("test_*.py")
    )


def plan(suites: list[Suite], tag: str | None, shards: int) -> list[Task]:
    """把"要跑什么"变成"要起几个进程"。分片按文件轮转，保证可复现。"""
    tasks: list[Task] = []
    for suite in suites:
        if not suite.supports(tag):
            continue
        files = discover_test_files(suite)
        if suite.shardable and files and shards > 1:
            buckets = [files[i::shards] for i in range(shards)]
            buckets = [bucket for bucket in buckets if bucket]
        else:
            buckets = [files] if files else [[]]

        for index, bucket in enumerate(buckets, start=1):
            tasks.append(
                Task(
                    suite_id=suite.id,
                    name=suite.name,
                    shard=index,
                    total_shards=len(buckets),
                    files=list(bucket),
                    kind=suite.kind,
                    timeout_s=suite.timeout_s,
                )
            )
    return tasks


# ---------------------------------------------------------------- 执行（run）


def _parse_junit(path: Path) -> dict[str, int]:
    if not path.exists():
        return {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    root = ET.parse(path).getroot()
    node = root if root.tag == "testsuite" else root.find("testsuite")
    if node is None:
        return {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    return {
        "tests": int(node.get("tests", 0)),
        "failures": int(node.get("failures", 0)),
        "errors": int(node.get("errors", 0)),
        "skipped": int(node.get("skipped", 0)),
    }


def execute(suite: Suite, task: Task, run_dir: Path) -> TaskResult:
    junit_path = run_dir / "junit" / f"{suite.id}-shard{task.shard}.xml"
    junit_path.parent.mkdir(parents=True, exist_ok=True)

    command = list(suite.command)
    if suite.kind == "test":
        command.append(f"--junitxml={junit_path}")
    command.extend(task.files)

    started = time.perf_counter()
    try:
        proc = subprocess.run(
            command,
            cwd=suite.cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=task.timeout_s,
        )
        elapsed = time.perf_counter() - started
        stats = _parse_junit(junit_path) if suite.kind == "test" else {}
        status = "PASS" if proc.returncode == 0 else "FAIL"
        message = ""
        if proc.returncode not in (0,):
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()
            message = tail[-1] if tail else ""
        return TaskResult(
            suite_id=suite.id,
            shard=task.shard,
            status=status,
            duration_s=round(elapsed, 2),
            junit=str(junit_path.relative_to(run_dir)) if junit_path.exists() else None,
            message=message,
            files=task.files,
            **stats,
        )
    except subprocess.TimeoutExpired:
        return TaskResult(
            suite_id=suite.id,
            shard=task.shard,
            status="TIMEOUT",
            duration_s=round(time.perf_counter() - started, 2),
            message=f"超过 {task.timeout_s}s 未结束，已强杀",
            files=task.files,
        )
    except Exception as exc:  # noqa: BLE001
        return TaskResult(
            suite_id=suite.id,
            shard=task.shard,
            status="ERROR",
            duration_s=round(time.perf_counter() - started, 2),
            message=f"{type(exc).__name__}: {exc}",
            files=task.files,
        )


def dispatch(suites: list[Suite], tasks: list[Task], run_dir: Path, parallel: int) -> list[TaskResult]:
    by_id = {suite.id: suite for suite in suites}
    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=max(1, parallel)) as pool:
        results = list(pool.map(lambda task: execute(by_id[task.suite_id], task, run_dir), tasks))
    for result in results:
        result.duration_s = result.duration_s or 0.0
    wall = time.perf_counter() - started
    print(f"  执行完成：{len(results)} 个任务，墙面耗时 {wall:.1f}s（并行度 {parallel}）")
    return results


# ---------------------------------------------------------------- 门禁（gate）


def evaluate(results: list[TaskResult], gate: Gate, wall_s: float) -> dict:
    total = sum(r.tests for r in results)
    failed = sum(r.failures + r.errors for r in results) + sum(1 for r in results if r.status in ("FAIL", "TIMEOUT", "ERROR"))
    pass_rate = (total - failed) / total if total else (1.0 if failed == 0 else 0.0)

    reasons: list[str] = []
    if failed > gate.max_failures:
        reasons.append(f"失败数 {failed} > 上限 {gate.max_failures}")
    if wall_s > gate.max_duration_s:
        reasons.append(f"耗时 {wall_s:.1f}s > 上限 {gate.max_duration_s:.0f}s")
    if pass_rate < gate.require_pass_rate:
        reasons.append(f"通过率 {pass_rate:.2%} < 要求 {gate.require_pass_rate:.0%}")
    for result in results:
        if result.status in ("TIMEOUT", "ERROR"):
            reasons.append(f"{result.suite_id}#{result.shard} {result.status}: {result.message}")

    return {
        "passed": not reasons,
        "reasons": reasons,
        "tests": total,
        "failed": failed,
        "pass_rate": round(pass_rate, 4),
        "wall_s": round(wall_s, 2),
    }


def new_run_id() -> str:
    return f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"


def as_dict(result: TaskResult) -> dict:
    return asdict(result)

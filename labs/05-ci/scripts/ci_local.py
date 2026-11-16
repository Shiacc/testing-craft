"""本地版"流水线编排器"：在单机上复现 CI 的分层、分片、并行与门禁。

它不是测试框架，也不是测试平台，而是 docs/01 §5.1 分层图里的**流程层**：
决定"跑什么、怎么切、并行多少、什么条件下红灯"。

用法：
    python scripts/ci_local.py                    # 快轨：unit + api + ui（冒烟）
    python scripts/ci_local.py --full             # 慢轨：跑全部用例（含 slow）
    python scripts/ci_local.py --shards 2         # 每个套件切成 2 片并行
    python scripts/ci_local.py --with-perf        # 追加性能门禁
    python scripts/ci_local.py --budget 180       # 总时长预算（秒），超时即失败

产物：labs/05-ci/.ci-results/ 下的 JUnit XML、summary.md、以及退出码。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

LABS = Path(__file__).resolve().parents[2]
RESULTS = Path(__file__).resolve().parents[1] / ".ci-results"
PY = sys.executable


@dataclass
class Suite:
    name: str
    cwd: Path
    marker: str | None = None
    extra_args: list[str] = field(default_factory=list)
    budget_s: float = 120.0


SUITES = {
    "unit": Suite("unit", LABS / "01-unit-pytest", marker="not slow"),
    "api": Suite("api", LABS / "02-api-contract"),
    "ui": Suite("ui", LABS / "03-ui-playwright"),
}


def discover_test_files(suite: Suite) -> list[Path]:
    return sorted((suite.cwd / "tests").glob("test_*.py"))


def build_command(suite: Suite, files: list[Path], report: Path, full: bool) -> list[str]:
    cmd = [PY, "-X", "utf8", "-m", "pytest", "-q", f"--junitxml={report}"]
    if suite.marker and not full:
        cmd += ["-m", suite.marker]
    cmd += [str(f.relative_to(suite.cwd)) for f in files]
    return cmd


def parse_junit(path: Path) -> dict[str, float]:
    if not path.exists():
        return {"tests": 0, "failures": 0, "errors": 0, "skipped": 0, "time": 0.0}
    root = ET.parse(path).getroot()
    node = root if root.tag == "testsuite" else root.find("testsuite")
    if node is None:
        return {"tests": 0, "failures": 0, "errors": 0, "skipped": 0, "time": 0.0}
    return {
        "tests": int(node.get("tests", 0)),
        "failures": int(node.get("failures", 0)),
        "errors": int(node.get("errors", 0)),
        "skipped": int(node.get("skipped", 0)),
        "time": float(node.get("time", 0.0)),
    }


def run_shard(suite: Suite, shard_id: int, files: list[Path], report: Path, full: bool) -> dict:
    started = time.perf_counter()
    proc = subprocess.run(
        build_command(suite, files, report, full),
        cwd=suite.cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    elapsed = time.perf_counter() - started
    stats = parse_junit(report)
    return {
        "suite": suite.name,
        "shard": shard_id,
        "files": [f.name for f in files],
        "returncode": proc.returncode,
        "elapsed": elapsed,
        "stdout_tail": proc.stdout.strip().splitlines()[-3:],
        **stats,
    }


def run_suite(suite: Suite, shards: int, full: bool) -> list[dict]:
    files = discover_test_files(suite)
    if not files:
        return []
    # 按文件轮转切分：简单、稳定、可复现；真实 CI 更推荐按历史耗时切分（见 docs/07 §7）
    buckets = [files[i::shards] for i in range(shards)]
    buckets = [b for b in buckets if b]

    RESULTS.mkdir(parents=True, exist_ok=True)
    jobs = []
    for index, bucket in enumerate(buckets, start=1):
        report = RESULTS / f"{suite.name}-shard{index}.xml"
        jobs.append((suite, index, bucket, report, full))

    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        return list(pool.map(lambda j: run_shard(*j), jobs))


def run_perf_gate() -> dict:
    started = time.perf_counter()
    proc = subprocess.run(
        [PY, "-X", "utf8", "py_load_smoke.py", "--concurrency", "10", "--requests", "300"],
        cwd=LABS / "04-perf",
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return {
        "name": "perf",
        "ok": proc.returncode == 0,
        "elapsed": time.perf_counter() - started,
        "detail": proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else "",
    }


def write_summary(rows: list[dict], perf: dict | None, total_elapsed: float, budget: float) -> Path:
    lines = ["# CI 运行汇总", "", f"- 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}", ""]
    lines += ["| 套件 | 分片 | 用例 | 失败 | 错误 | 跳过 | 耗时(s) |", "|---|---|---|---|---|---|---|"]
    totals = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    for row in rows:
        lines.append(
            f"| {row['suite']} | {row['shard']} | {row['tests']} | {row['failures']} | "
            f"{row['errors']} | {row['skipped']} | {row['elapsed']:.2f} |"
        )
        for key in totals:
            totals[key] += row[key]

    failed = totals["failures"] + totals["errors"]
    lines += [
        "",
        f"**合计**：用例 {totals['tests']}，失败 {failed}，跳过 {totals['skipped']}，"
        f"墙面耗时 {total_elapsed:.1f}s（预算 {budget:.0f}s）",
        "",
    ]
    if perf:
        lines.append(f"- 性能门禁：{'PASS' if perf['ok'] else 'FAIL'}（{perf['elapsed']:.1f}s）")
    lines.append(f"- 门禁结论：{'通过' if failed == 0 and total_elapsed <= budget else '未通过'}")

    path = RESULTS / "summary.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="本地流水线：分层 + 分片 + 并行 + 门禁")
    parser.add_argument("--shards", type=int, default=1, help="每个套件切分片数")
    parser.add_argument("--full", action="store_true", help="跑全部用例（含 slow 标记）")
    parser.add_argument("--with-perf", action="store_true", help="追加性能门禁")
    parser.add_argument("--budget", type=float, default=300.0, help="墙面耗时预算（秒）")
    parser.add_argument("--suites", default="unit,api,ui", help="要跑的套件，逗号分隔")
    args = parser.parse_args(argv)

    selected = [name.strip() for name in args.suites.split(",") if name.strip() in SUITES]
    if not selected:
        print("没有匹配的套件")
        return 2

    RESULTS.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    rows: list[dict] = []

    print(f"== 快轨{'（慢轨）' if args.full else ''}：{', '.join(selected)}，分片 {args.shards} ==")

    with ThreadPoolExecutor(max_workers=max(1, len(selected))) as pool:
        for suite_rows in pool.map(lambda name: run_suite(SUITES[name], args.shards, args.full), selected):
            rows.extend(suite_rows)

    perf = None
    if args.with_perf:
        print("== 性能门禁 ==")
        perf = run_perf_gate()
        print(f"  {perf['name']}: {'PASS' if perf['ok'] else 'FAIL'}  {perf['detail']}")

    total_elapsed = time.perf_counter() - started
    summary = write_summary(rows, perf, total_elapsed, args.budget)

    print("\n== 结果 ==")
    for row in sorted(rows, key=lambda r: (r["suite"], r["shard"])):
        status = "PASS" if row["returncode"] == 0 else "FAIL"
        print(
            f"  [{status}] {row['suite']}#{row['shard']}  "
            f"用例 {row['tests']:>2}  失败 {row['failures']}  耗时 {row['elapsed']:.1f}s  "
            f"files={','.join(row['files'])}"
        )

    failed = sum(r["failures"] + r["errors"] for r in rows) + sum(1 for r in rows if r["returncode"] not in (0, 1))
    ok = failed == 0 and total_elapsed <= args.budget and (perf is None or perf["ok"])

    print(f"\n墙面耗时 {total_elapsed:.1f}s / 预算 {args.budget:.0f}s")
    print(f"汇总报告：{summary}")
    print("门禁：" + ("通过" if ok else "未通过"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
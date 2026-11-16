"""平台 v0.1 的命令行入口。

子命令刻意对齐真实平台的操作语义：
    list     看资产（登记了什么、谁负责）
    plan     只调度不执行，用于检查"切出来的是不是我要的"
    run      执行 + 门禁 + 报告
    history  看趋势

用法：
    python -m platform_lite list
    python -m platform_lite plan --tag smoke --shards 2
    python -m platform_lite run --tag smoke --parallel 4 --shards 2
    python -m platform_lite run --suite unit,api --env ci
    python -m platform_lite history --limit 5
"""

from __future__ import annotations

import argparse
import dataclasses
import sys
import time
from datetime import datetime
from pathlib import Path

from . import registry, report, runner

BASE = Path(__file__).resolve().parents[1]
RUNS = BASE / "runs"


def _load():
    try:
        return registry.load()
    except registry.RegistryError as exc:
        print(f"登记表错误: {exc}")
        raise SystemExit(2)


def cmd_list(args: argparse.Namespace) -> int:
    suites, gate, environments = _load()
    print(f"环境: {', '.join(environments) or '(未定义)'}")
    print(f"门禁: 失败上限 {gate.max_failures}，耗时上限 {gate.max_duration_s:.0f}s，通过率下限 {gate.require_pass_rate:.0%}\n")
    print(f"{'ID':<6} {'类型':<6} {'分片':<5} {'标签':<24} {'负责人':<16} 名称")
    for suite in suites:
        print(
            f"{suite.id:<6} {suite.kind:<6} {'是' if suite.shardable else '否':<5} "
            f"{','.join(suite.tags):<24} {suite.owner:<16} {suite.name}"
        )
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    suites, _, _ = _load()
    tasks = runner.plan(suites, args.tag, args.shards)
    if not tasks:
        print("没有匹配的套件（检查 --tag）")
        return 1
    print(f"共 {len(tasks)} 个任务：")
    for task in tasks:
        files = f"  files={','.join(task.files)}" if task.files else ""
        print(f"  - {task.suite_id}#{task.shard}/{task.total_shards}  kind={task.kind}  timeout={task.timeout_s}s{files}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    suites, gate, environments = _load()
    if args.env not in environments:
        print(f"未知环境: {args.env}（可选: {', '.join(environments)}）")
        return 2

    selected = [s for s in suites if not args.suite or s.id in args.suite.split(",")]
    if not selected:
        print("没有匹配的套件")
        return 1

    tasks = runner.plan(selected, args.tag, args.shards)
    if not tasks:
        print("没有匹配的套件（检查 --tag）")
        return 1

    run_id = args.run_id or runner.new_run_id()
    run_dir = RUNS / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    started_at = datetime.now().strftime(runner.ISO)
    print(f"run_id={run_id}  环境={args.env}  并行={args.parallel}  分片={args.shards}  标签={args.tag or '全部'}")
    print(f"任务数 {len(tasks)}，产物目录 {run_dir}")

    wall_started = time.perf_counter()
    results = runner.dispatch(selected, tasks, run_dir, args.parallel)
    wall_s = time.perf_counter() - wall_started

    # 门禁阈值允许按环境覆盖：CI 上通常比本地更严（docs/08 §5 分级门禁）
    if args.max_duration is not None or args.max_failures is not None:
        gate = dataclasses.replace(
            gate,
            max_duration_s=gate.max_duration_s if args.max_duration is None else args.max_duration,
            max_failures=gate.max_failures if args.max_failures is None else args.max_failures,
        )

    gate_result = runner.evaluate(results, gate, wall_s)
    payload = {
        "run_id": run_id,
        "environment": args.env,
        "parallel": args.parallel,
        "shard_count": args.shards,
        "tag": args.tag,
        "started_at": started_at,
        "finished_at": datetime.now().strftime(runner.ISO),
        "results": [runner.as_dict(result) for result in results],
        "gate": gate_result,
    }

    report.summarize(results)
    report.write_results(run_dir, payload)
    html_path = report.write_report(run_dir, payload)
    report.append_history({**payload["gate"], "run_id": run_id, "environment": args.env})

    print(f"\n门禁：{'通过' if gate_result['passed'] else '未通过'}")
    for reason in gate_result["reasons"]:
        print(f"  - {reason}")
    print(f"报告：{html_path}")
    return 0 if gate_result["passed"] else 1


def cmd_history(args: argparse.Namespace) -> int:
    rows = report._history_rows(args.limit)  # noqa: SLF001 - 同包内复用
    if not rows:
        print("暂无历史记录，先跑一次 run")
        return 0
    print(f"{'run_id':<24} {'用例':>4} {'失败':>4} {'通过率':>7} {'耗时':>7} 门禁")
    for row in rows:
        print(
            f"{row.get('run_id', '-'):<24} {row.get('tests', 0):>4} {row.get('failed', 0):>4} "
            f"{row.get('pass_rate', 0):>6.1%} {row.get('wall_s', 0):>6.1f}s "
            f"{'通过' if row.get('passed') else '未通过'}"
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="platform_lite", description="最小可用测试平台 v0.1")
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="列出登记的用例资产")
    p_list.set_defaults(func=cmd_list)

    p_plan = sub.add_parser("plan", help="只调度不执行")
    p_plan.add_argument("--tag", default=None)
    p_plan.add_argument("--shards", type=int, default=1)
    p_plan.set_defaults(func=cmd_plan)

    p_run = sub.add_parser("run", help="执行并出报告")
    p_run.add_argument("--tag", default=None, help="按标签筛选，如 smoke")
    p_run.add_argument("--suite", default=None, help="逗号分隔的套件 id")
    p_run.add_argument("--shards", type=int, default=1)
    p_run.add_argument("--parallel", type=int, default=2, help="执行机（并发任务）数量")
    p_run.add_argument("--env", default="local")
    p_run.add_argument("--max-duration", type=float, default=None, help="覆盖耗时门禁（秒）")
    p_run.add_argument("--max-failures", type=int, default=None, help="覆盖失败数门禁")
    p_run.add_argument("--run-id", default=None)
    p_run.set_defaults(func=cmd_run)

    p_hist = sub.add_parser("history", help="查看历史趋势")
    p_hist.add_argument("--limit", type=int, default=10)
    p_hist.set_defaults(func=cmd_history)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())

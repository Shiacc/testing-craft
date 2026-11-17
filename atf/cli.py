"""atf 命令行入口。

    python -m atf validate                 # 用例库校验（含测试绑定是否真实存在）
    python -m atf coverage                 # 分层/优先级覆盖率
    python -m atf list                     # 用例清单
    python -m atf run --tag smoke          # 执行并回写用例库 + 出报告
    python -m atf run --canary             # 含演练用负向用例（预期失败）
    python -m atf file-bugs --dry-run      # 从最近一次运行生成缺陷草稿（不提交）
    python -m atf file-bugs --tracker jira # 真实提交（需凭据，缺失则拒绝并说明）

退出码约定：0 通过 / 1 有失败 / 2 配置或用例库不合法 / 3 环境不满足。
"""

from __future__ import annotations

import argparse
import json
import sys

from . import bugflow, report, runner
from .catalog import Catalog

ROOT_MARK = "atf"


def _project(project_id: str | None):
    from . import project as project_mod

    return project_mod.load().get(project_id)


def _load_or_exit(project_id: str | None = None) -> Catalog:
    try:
        project = _project(project_id)
        return Catalog.load(project.cases, lab_dir=project.suites_cwd)
    except FileNotFoundError:
        print("找不到用例库文件")
        raise SystemExit(2)
    except Exception as exc:  # noqa: BLE001
        print(f"项目/用例库加载失败：{exc}")
        raise SystemExit(2)


def cmd_validate(args: argparse.Namespace) -> int:
    catalog = _load_or_exit(args.project)
    errors, warnings = catalog.validate()
    print(f"用例总数：{len(catalog.cases)}")
    for warning in warnings:
        print(f"[WARN] {warning}")
    for error in errors:
        print(f"[FAIL] {error}")
    print(f"\n结论：{'通过' if not errors else f'{len(errors)} 项失败'}"
          f"（WARN {len(warnings)} 项）")
    return 0 if not errors else 2


def cmd_coverage(args: argparse.Namespace) -> int:
    catalog = _load_or_exit(args.project)
    cov = catalog.coverage()
    overall = cov["overall"]
    print(f"总覆盖率：{overall['rate']:.0%}（{overall['automated']}/{overall['total']}）")
    print(f"绑定测试：{cov['bound_tests']} / 收集到 {cov['collected_tests']}")
    print("\n按层级：")
    for layer, stat in cov["by_layer"].items():
        if stat["total"]:
            print(f"  {layer}: {stat['automated']}/{stat['total']} = {stat['rate']:.0%}")
    print("\n按优先级：")
    for priority, stat in cov["by_priority"].items():
        if stat["total"]:
            print(f"  {priority}: {stat['automated']}/{stat['total']} = {stat['rate']:.0%}")
    if args.json:
        print(json.dumps(cov, ensure_ascii=False, indent=2))
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    catalog = _load_or_exit(args.project)
    print(f"{'ID':<8} {'层':<4} {'优先级':<4} {'状态':<10} {'最近结果':<8} 标题")
    for case in catalog.cases:
        print(
            f"{case.id:<8} {case.layer:<4} {case.priority:<4} {case.status:<10} "
            f"{(case.last_result or '-'):<8} {case.title}"
        )
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    catalog = _load_or_exit(args.project)
    print(f"执行：tag={args.tag or '全部'} canary={args.canary}")
    project = _project(args.project)
    result = runner.run(
        tag=args.tag,
        canary=args.canary,
        catalog=catalog,
        extra_args=args.pytest_args,
        target_url=args.target_url,
        lab_dir=project.suites_cwd,
    )
    counts = result.counts()

    filing = None
    if args.file_bugs or args.dry_run:
        filing = bugflow.file_failures(
            result,
            catalog,
            tracker=args.tracker,
            dry_run=args.dry_run or not bugflow.tracker_ready(args.tracker)[0],
            base_url=_base_url(catalog),
        )

    run_dir = report.write(result, catalog, filing)
    print(
        f"\n结果：pass {counts['pass']} / fail {counts['fail']} / error {counts['error']} / skip {counts['skip']}"
        f"（{result.duration_s:.1f}s）"
    )
    if result.unbound:
        print(f"黑户测试 {len(result.unbound)} 条，见报告")
    if filing:
        for item in filing:
            print(f"  [{item.action}] {item.case_id or '-'} {item.key or ''} {item.detail[:80]}")
    print(f"报告：{run_dir / 'summary.md'}")
    return 0 if counts["fail"] == 0 and counts["error"] == 0 else 1


def _base_url(catalog: Catalog) -> str:
    target = catalog.data.get("target") or {}
    return str(target.get("base_url") or "http://127.0.0.1:8099")


def _latest_run() -> tuple[Catalog, object]:
    catalog = _load_or_exit()
    runs = sorted((runner.RUNS_DIR).glob("*/results.json"))
    if not runs:
        print("还没有运行记录，先执行 python -m atf run")
        raise SystemExit(3)
    payload = json.loads(runs[-1].read_text(encoding="utf-8"))

    class _Result:
        pass

    result = _Result()
    result.run_id = payload["run_id"]
    result.failures = [
        runner.Outcome(
            nodeid=item["nodeid"],
            name=item["nodeid"].split("::")[-1],
            status=item["status"],
            message=item.get("message", ""),
            case_id=item.get("case_id", ""),
        )
        for item in payload["cases"]
        if item["status"] in ("fail", "error")
    ]
    return catalog, result


def cmd_file_bugs(args: argparse.Namespace) -> int:
    catalog, result = _latest_run()
    ready, why = bugflow.tracker_ready(args.tracker)
    dry_run = args.dry_run or not ready
    if not ready and not args.dry_run:
        print(f"[WARN] {args.tracker} 凭据不完整，自动降级为 dry-run：{why}")

    filing = bugflow.file_failures(
        result,
        catalog,
        tracker=args.tracker,
        dry_run=dry_run,
        base_url=_base_url(catalog),
    )
    if not filing:
        print("最近一次运行没有可提缺陷的失败项")
        return 0
    print(f"{'用例':<8} {'动作':<11} {'系统':<7} {'单号':<10} 说明")
    for item in filing:
        print(f"{item.case_id or '-':<8} {item.action:<11} {item.tracker or '-':<7} {item.key or '-':<10} {item.detail[:70]}")
    return 0


def cmd_e2e(args: argparse.Namespace) -> int:
    """转发给 atf.e2e（端到端编排），保持命令入口唯一。"""
    from . import e2e

    forwarded: list[str] = []
    if args.project:
        forwarded += ["--project", args.project]
    if args.config:
        forwarded += ["--config", args.config]
    if args.filing:
        forwarded += ["--filing", args.filing]
    if args.tracker:
        forwarded += ["--tracker", args.tracker]
    if args.port:
        forwarded += ["--port", str(args.port)]
    if args.no_reset:
        forwarded += ["--no-reset"]
    if args.keep_server:
        forwarded += ["--keep-server"]
    if args.repeat and args.repeat != 1:
        forwarded += ["--repeat", str(args.repeat)]
    if args.e2e_extra:
        forwarded += args.e2e_extra
    return e2e.main(forwarded)



def cmd_tracker(args: argparse.Namespace) -> int:
    """缺陷系统体检：真实提交前先 GET 一次元数据。"""
    import sys as _sys

    from . import tracker as tracker_mod

    trackers = ["zentao", "jira"] if args.tracker == "both" else [args.tracker]
    if args.mock:
        print("[mock] 启动本地 mock 缺陷服务，验证体检逻辑本身可用（不代表真实系统可用）")

    results = tracker_mod.run(trackers, mock=args.mock)
    print(f"{'系统':<8} {'检查项':<10} 结果   说明")
    for item in results:
        print(f"{item.tracker:<8} {item.name:<10} {'OK  ' if item.ok else 'FAIL'}  {item.detail[:90]}")

    failed = [item for item in results if not item.ok]
    print()
    if failed:
        print(f"结论：{len(failed)} 项未通过")
        if any("缺少环境变量" in item.detail for item in failed):
            print("  → 先按 docs/15-runbook.md §4 配置环境变量，再重跑本命令")
        return 3
    print("结论：通过（可以试 --filing submit）")
    return 0


def _add_project_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--project", default=None, help="项目 ID（见 atf/projects.yaml，默认取 default）")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="atf", description="用例库 + 执行 + 缺陷联动")
    sub = parser.add_subparsers(dest="command", required=True)

    p_validate = sub.add_parser("validate", help="校验用例库与测试绑定")
    _add_project_arg(p_validate)
    p_validate.set_defaults(func=cmd_validate)

    p_cov = sub.add_parser("coverage", help="覆盖率统计")
    _add_project_arg(p_cov)
    p_cov.add_argument("--json", action="store_true")
    p_cov.set_defaults(func=cmd_coverage)

    p_list = sub.add_parser("list", help="列出用例")
    _add_project_arg(p_list)
    p_list.set_defaults(func=cmd_list)

    p_run = sub.add_parser("run", help="执行测试并回写用例库")
    p_run.add_argument("--tag", default=None, help="pytest marker，如 smoke / l3 / canary")
    p_run.add_argument("--canary", action="store_true", help="启用演练用负向用例")
    p_run.add_argument("--file-bugs", action="store_true", help="失败后进入缺陷流")
    p_run.add_argument("--dry-run", action="store_true", help="缺陷流只演练不提交")
    p_run.add_argument("--tracker", default="zentao", choices=["zentao", "jira"])
    p_run.add_argument("--target-url", default=None, help="覆盖被测地址（注入 QA_BASE_URL 给套件）")
    _add_project_arg(p_run)
    p_run.add_argument("pytest_args", nargs="*", help="透传给 pytest 的额外参数")
    p_run.set_defaults(func=cmd_run)

    p_e2e = sub.add_parser("e2e", help="端到端：重置→起服务→分层执行→归因→缺陷→报告")
    _add_project_arg(p_e2e)
    p_e2e.add_argument("--config", default=None, help="流程配置（默认 atf/config/e2e.yaml）")
    p_e2e.add_argument("--filing", default=None, choices=["dry-run", "mock", "submit"])
    p_e2e.add_argument("--tracker", default=None, choices=["zentao", "jira", "both"])
    p_e2e.add_argument("--port", type=int, default=None)
    p_e2e.add_argument("--no-reset", action="store_true")
    p_e2e.add_argument("--keep-server", action="store_true")
    p_e2e.add_argument("--repeat", type=int, default=1, help="连跑 N 次并对比一致性")
    p_e2e.add_argument("e2e_extra", nargs="*", help="透传给 atf e2e 的额外参数")
    p_e2e.set_defaults(func=cmd_e2e)

    p_tracker = sub.add_parser("tracker", help="缺陷系统体检（GET 元数据，验证项目/字段可用）")
    p_tracker.add_argument("action", nargs="?", default="check", choices=["check"])
    p_tracker.add_argument("--tracker", default="both", choices=["zentao", "jira", "both"])
    p_tracker.add_argument("--mock", action="store_true", help="起本地 mock，验证体检逻辑本身")
    p_tracker.set_defaults(func=cmd_tracker)

    p_bug = sub.add_parser("file-bugs", help="从最近一次运行生成/提交缺陷")
    p_bug.add_argument("--tracker", default="zentao", choices=["zentao", "jira"])
    p_bug.add_argument("--dry-run", action="store_true")
    p_bug.set_defaults(func=cmd_file_bugs)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
"""bugfiler 的命令行入口：手工提单、查重、起 mock 服务。

为什么要有 CLI（明明主流程是直接调 Python API）：
缺陷联动一旦进 CI，出问题的现场往往只剩下一段日志。此时最需要的是"用刚才那条失败
再打一次真实接口、看对方到底返回什么"。CLI 把同一套 BugDraft / 客户端代码暴露成可
单步复现的命令，且**默认不打印任何凭据**（只显示 ``***abcd`` 形态），可以直接贴进
工单或群聊。

退出码约定（方便以后被 shell 脚本调用）：
    0  成功（file 提交成功 / dedupe-check 未命中 / mock 正常退出）
    1  链路失败（BugFilerError：认证、4xx、超时）/ dedupe-check 已存在记录
    2  用法或参数问题

用法：
    # 起 mock 服务，另开一个终端照它提单
    python -m atf.bugfiler.cli mock --port 8765

    # 提交一张缺陷（禅道，账号密码换 token）
    python -m atf.bugfiler.cli file --tracker zentao --base-url http://127.0.0.1:8765 \\
        --product-id 1 --account qa_bot --password s3cret \\
        --title "结算页优惠券未生效" --description "满 100 减 20 未命中" \\
        --case-id CAT-001 --test-id tests/test_checkout.py::test_coupon --actual "payable=100.00"

    # 只查指纹是否已提过
    python -m atf.bugfiler.cli dedupe-check --case-id CAT-001 --actual "payable=100.00"
"""

from __future__ import annotations

import argparse
import os
import sys

from .dedupe import DEFAULT_STATE_PATH, DedupeStore, describe
from .jira import JiraClient
from .mock_server import ThreadedMockServer
from .models import SEVERITIES, BugDraft, BugFilerError, fingerprint_for
from .zentao import ZenTaoClient

# ---------------------------------------------------------------- 辅助


class _UsageError(RuntimeError):
    """参数组合不完整（如选了禅道却没给 --product-id）。

    不直接 raise SystemExit：那会把"用法错误"和 argparse 内部的 --help 退出混在一起，
    退出码变得不可预测。这里只负责把消息带回 main，由它统一映射成 2。
    """


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def _build_draft(args: argparse.Namespace) -> BugDraft:
    return BugDraft(
        title=args.title,
        description=args.description,
        case_id=args.case_id,
        test_id=args.test_id,
        severity=args.severity,
        priority=args.priority,
        environment=args.environment,
        steps=list(args.step or []),
        expected=args.expected,
        actual=args.actual,
        attachments=list(args.attachment or []),
        labels=list(args.label or []),
    )


def _build_client(args: argparse.Namespace):
    """按 --tracker 组装客户端；凭据优先取命令行，其次环境变量（避免进 shell 历史）。"""
    if args.tracker == "zentao":
        if not args.base_url:
            raise _UsageError("禅道需要 --base-url（如 http://127.0.0.1:8765）")
        if not args.product_id:
            raise _UsageError("禅道需要 --product-id（缺陷归属的产品）")
        client = ZenTaoClient(
            args.base_url,
            product_id=args.product_id,
            token=args.token or _env("ZENTAO_TOKEN"),
            account=args.account or _env("ZENTAO_ACCOUNT"),
            password=args.password or _env("ZENTAO_PASSWORD"),
            timeout=args.timeout,
        )
        # 账号密码模式下此时还没换到 token；明确说清楚，免得看日志的人以为漏配了凭据。
        token_state = client.token_hint if client.token_hint != "(未设置)" else "提交时用 account 换取"
        print(f"目标：禅道 {client.base_url}  产品={client.product_id}  token={token_state}")
        return client

    if not args.base_url:
        raise _UsageError("Jira 需要 --base-url（如 https://example.atlassian.net）")
    if not args.project_key:
        raise _UsageError("Jira 需要 --project-key（如 QA）")
    client = JiraClient(
        args.base_url,
        email=args.email or _env("JIRA_EMAIL"),
        api_token=args.api_token or _env("JIRA_API_TOKEN"),
        project_key=args.project_key,
        issue_type=args.issue_type,
        timeout=args.timeout,
    )
    print(f"目标：Jira {client.base_url}  项目={client.project_key}  token={client.token_hint}")
    return client


# ---------------------------------------------------------------- 子命令


def cmd_file(args: argparse.Namespace) -> int:
    draft = _build_draft(args)
    store = DedupeStore(args.state)
    print(f"指纹：{draft.fingerprint}")
    print(f"去重表：{store.path}")

    existing = store.check(draft.fingerprint)
    if existing and not args.force:
        print(f"命中去重，跳过提交：{describe(existing)}")
        print("（确认要重提请加 --force）")
        return 0

    if args.dry_run:
        # dry-run 先于客户端构造：只核对"将要提交什么"时不该要求提供凭据。
        print("[dry-run] 只打印将要提交的草稿，不发请求")
        for key, value in draft.as_dict().items():
            print(f"  {key} = {value}")
        return 0

    client = _build_client(args)
    result = client.create(draft)
    print(f"已提交：{result['tracker']}:{result['key']} {result['url']}")
    if args.no_record:
        print("（--no-record：未写入去重表）")
        return 0
    record = store.record(draft.fingerprint, result["tracker"], result["key"], result["url"])
    print(f"已记录去重表：{describe(record)}")
    return 0


def cmd_dedupe_check(args: argparse.Namespace) -> int:
    fingerprint = fingerprint_for(args.case_id, args.test_id, args.actual)
    store = DedupeStore(args.state)
    print(f"指纹：{fingerprint}")
    print(f"去重表：{store.path}")
    existing = store.check(fingerprint)
    if existing is None:
        print("未提交过（可以提）")
        return 0
    print(f"已存在：{describe(existing)}")
    return 1


def cmd_mock(args: argparse.Namespace) -> int:
    server = ThreadedMockServer(args.port, token=args.token, quiet=not args.verbose)
    with server:
        print(f"mock 缺陷服务已启动：{server.base_url}")
        print("禅道 /api.php/v1/tokens、/api.php/v1/bugs；Jira /rest/api/3/issue；Ctrl+C 停止")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\n已停止")
    return 0


# ---------------------------------------------------------------- 解析


def _add_draft_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--title", required=True, help="缺陷标题（≤120 字，超出自动截断）")
    parser.add_argument("--description", required=True, help="缺陷描述（Markdown 或纯文本）")
    parser.add_argument("--case-id", default="", help="用例库 ID，如 CAT-001")
    parser.add_argument("--test-id", default="", help="pytest nodeid")
    parser.add_argument("--severity", default="一般", choices=list(SEVERITIES), help="禅道口径严重程度")
    parser.add_argument("--priority", type=int, default=3, help="1-4（1 最紧急）")
    parser.add_argument("--environment", default="", help="环境名，如 prod-readonly")
    parser.add_argument("--step", action="append", default=[], help="复现步骤（可重复）")
    parser.add_argument("--expected", default="", help="期望结果")
    parser.add_argument("--actual", default="", help="实际结果 / 断言消息")
    parser.add_argument("--attachment", action="append", default=[], help="附件本地路径（可重复）")
    parser.add_argument("--label", action="append", default=[], help="标签（可重复）")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="atf.bugfiler", description="缺陷联动：失败自动提禅道 / Jira")
    sub = parser.add_subparsers(dest="command", required=True)

    p_file = sub.add_parser("file", help="提交一张缺陷（先去重，再提交，最后记录）")
    p_file.add_argument("--tracker", required=True, choices=["zentao", "jira"])
    _add_draft_args(p_file)
    p_file.add_argument("--state", default=str(DEFAULT_STATE_PATH), help="去重表路径")
    p_file.add_argument("--force", action="store_true", help="忽略去重命中，强制重提")
    p_file.add_argument("--no-record", action="store_true", help="提交后不写去重表")
    p_file.add_argument("--dry-run", action="store_true", help="只打印草稿，不发请求")
    p_file.add_argument("--timeout", type=float, default=10.0, help="单次请求超时（秒）")
    p_file.add_argument("--base-url", default="", help="缺陷系统根地址")
    p_file.add_argument("--product-id", type=int, default=0, help="禅道产品 ID")
    p_file.add_argument("--token", default="", help="禅道 token（或环境变量 ZENTAO_TOKEN）")
    p_file.add_argument("--account", default="", help="禅道账号（或环境变量 ZENTAO_ACCOUNT）")
    p_file.add_argument("--password", default="", help="禅道密码（或环境变量 ZENTAO_PASSWORD）")
    p_file.add_argument("--email", default="", help="Jira 账号邮箱（或环境变量 JIRA_EMAIL）")
    p_file.add_argument("--api-token", default="", help="Jira API Token（或环境变量 JIRA_API_TOKEN）")
    p_file.add_argument("--project-key", default="", help="Jira 项目 key，如 QA")
    p_file.add_argument("--issue-type", default="Bug", help="Jira 工作项类型，默认 Bug")
    p_file.set_defaults(func=cmd_file)

    p_check = sub.add_parser("dedupe-check", help="只查指纹是否已提过（0=未提过，1=已存在）")
    p_check.add_argument("--case-id", default="")
    p_check.add_argument("--test-id", default="")
    p_check.add_argument("--actual", default="")
    p_check.add_argument("--state", default=str(DEFAULT_STATE_PATH))
    p_check.set_defaults(func=cmd_dedupe_check)

    p_mock = sub.add_parser("mock", help="启动本地 mock 缺陷服务（手工调试用）")
    p_mock.add_argument("--port", type=int, default=8765)
    p_mock.add_argument("--token", default="mock-token")
    p_mock.add_argument("--verbose", action="store_true", help="打印访问日志")
    p_mock.set_defaults(func=cmd_mock)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except BugFilerError as exc:
        # 只打异常文本：状态码与脱敏后的响应片段已经在消息里了。
        print(f"[失败] {exc}")
        return 1
    except _UsageError as exc:
        print(f"[用法错误] {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())

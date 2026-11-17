"""缺陷流：把"失败的用例"变成"可提交的缺陷草稿"。

四步流水线：
    失败项 → 归因（用例 ID / 层级 / 优先级） → 构造 BugDraft（含证据） → 指纹去重 → 提交

三条原则：
1. **没有用例 ID 的失败不进缺陷流**：那说明它是黑户测试，先补用例，否则缺陷无法回归；
2. **默认 dry-run**：没有真实凭据时绝不假装提交成功，输出"将会提交什么"；
3. **指纹去重**：同一用例同一失败签名只提一次，避免每天刷同一个 bug。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from atf.bugfiler.dedupe import DedupeStore
from atf.bugfiler.jira import JiraClient
from atf.bugfiler.models import BugDraft, BugFilerError
from atf.bugfiler.zentao import ZenTaoClient
from atf.catalog import ROOT, Catalog
from atf.runner import RunResult

STATE_PATH = ROOT / "atf" / "state" / "filed_bugs.json"

SEVERITY_BY_PRIORITY = {"P0": "严重", "P1": "一般", "P2": "轻微"}
JIRA_PRIORITY = {"P0": 1, "P1": 3, "P2": 4}


@dataclass
class FilingOutcome:
    case_id: str
    fingerprint: str
    action: str  # filed / duplicate / skipped / would-file / error
    tracker: str = ""
    key: str = ""
    url: str = ""
    detail: str = ""


def build_draft(outcome, catalog: Catalog, base_url: str) -> BugDraft | None:
    case = catalog.by_id(outcome.case_id)
    if case is None:
        return None
    failure = (outcome.message or "断言失败，详情见运行日志").strip()
    description = "\n".join(
        [
            f"## 环境\n- 站点：{base_url}\n- 执行时间：{outcome.nodeid and '见运行记录'}\n- 层级：{case.layer}",
            f"## 复现步骤\n1. 运行自动化用例：`{case.automated_by}`\n2. 观察断言结果",
            f"## 期望\n{case.title}",
            f"## 实际\n{failure[:400]}",
            f"## 证据\n- 用例 ID：{case.id}\n- 测试 nodeid：`{outcome.nodeid}`\n- 归属：{case.owner}",
            "## 判定依据\n自动化断言失败；非环境问题（若为环境问题请标注并跳过本缺陷）。",
        ]
    )
    draft = BugDraft(
        title=f"[{case.layer}] {case.title}（{case.id}）",
        description=description,
        case_id=case.id,
        test_id=outcome.nodeid,
        severity=SEVERITY_BY_PRIORITY.get(case.priority, "一般"),
        priority=JIRA_PRIORITY.get(case.priority, 3),
        environment=f"wagtail-demo @ {base_url}",
        steps=[f"执行 {case.automated_by}", "断言失败"],
        expected=case.title,
        actual=failure[:300],
        labels=[case.layer.lower(), "automation"],
    )
    draft.fingerprint = draft.compute_fingerprint()
    return draft


def file_failures(
    result: RunResult,
    catalog: Catalog,
    *,
    tracker: str = "zentao",
    dry_run: bool = True,
    state_path: Path | None = None,
    base_url: str = "http://127.0.0.1:8099",
) -> list[FilingOutcome]:
    store = DedupeStore(state_path or STATE_PATH)
    outcomes: list[FilingOutcome] = []

    for failure in result.failures:
        if not failure.case_id:
            outcomes.append(
                FilingOutcome(
                    case_id="",
                    fingerprint="",
                    action="skipped",
                    detail=f"{failure.nodeid} 没有对应用例，先补用例再谈缺陷",
                )
            )
            continue

        draft = build_draft(failure, catalog, base_url)
        if draft is None:
            continue

        existing = store.check(draft.fingerprint)
        if existing:
            outcomes.append(
                FilingOutcome(
                    case_id=draft.case_id,
                    fingerprint=draft.fingerprint,
                    # dry-run 下如实说明"已存在，将跳过"，避免被误读成"这次要提交"
                    action="would-duplicate" if dry_run else "duplicate",
                    tracker=existing.get("tracker", ""),
                    key=existing.get("key", ""),
                    url=existing.get("url", ""),
                    detail="同一指纹已提过，跳过",
                )
            )
            continue

        if dry_run:
            outcomes.append(
                FilingOutcome(
                    case_id=draft.case_id,
                    fingerprint=draft.fingerprint,
                    action="would-file",
                    tracker=tracker,
                    detail=f"标题：{draft.title}｜严重级：{draft.severity}",
                )
            )
            continue

        try:
            record = _submit(tracker, draft)
        except BugFilerError as exc:
            outcomes.append(
                FilingOutcome(
                    case_id=draft.case_id,
                    fingerprint=draft.fingerprint,
                    action="error",
                    tracker=tracker,
                    detail=str(exc)[:300],
                )
            )
            continue

        store.record(draft.fingerprint, tracker, record["key"], record["url"])
        outcomes.append(
            FilingOutcome(
                case_id=draft.case_id,
                fingerprint=draft.fingerprint,
                action="filed",
                tracker=tracker,
                key=record["key"],
                url=record["url"],
            )
        )

    return outcomes


def _submit(tracker: str, draft: BugDraft) -> dict:
    if tracker == "zentao":
        client = ZenTaoClient(
            os.environ.get("ZENTAO_BASE_URL", ""),
            product_id=int(os.environ.get("ZENTAO_PRODUCT_ID", "0") or 0),
            token=os.environ.get("ZENTAO_TOKEN", ""),
            account=os.environ.get("ZENTAO_ACCOUNT", ""),
            password=os.environ.get("ZENTAO_PASSWORD", ""),
        )
        return client.create(draft)
    if tracker == "jira":
        client = JiraClient(
            os.environ.get("JIRA_BASE_URL", ""),
            email=os.environ.get("JIRA_EMAIL", ""),
            api_token=os.environ.get("JIRA_API_TOKEN", ""),
            project_key=os.environ.get("JIRA_PROJECT_KEY", ""),
        )
        return client.create(draft)
    raise BugFilerError(f"未知缺陷系统：{tracker}（可选 zentao / jira）")


def tracker_ready(tracker: str) -> tuple[bool, str]:
    """检查真实提交所需的凭据是否齐全。缺失时主流程必须走 dry-run 并如实标注。"""
    if tracker == "zentao":
        required = ["ZENTAO_BASE_URL", "ZENTAO_PRODUCT_ID"]
        optional = ["ZENTAO_TOKEN"] + ["ZENTAO_ACCOUNT", "ZENTAO_PASSWORD"]
        missing = [name for name in required if not os.environ.get(name)]
        if not os.environ.get("ZENTAO_TOKEN") and not (
            os.environ.get("ZENTAO_ACCOUNT") and os.environ.get("ZENTAO_PASSWORD")
        ):
            missing.append("ZENTAO_TOKEN 或 (ZENTAO_ACCOUNT + ZENTAO_PASSWORD)")
        return (not missing, "缺少环境变量：" + "、".join(missing) if missing else "")
    if tracker == "jira":
        required = ["JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN", "JIRA_PROJECT_KEY"]
        missing = [name for name in required if not os.environ.get(name)]
        return (not missing, "缺少环境变量：" + "、".join(missing) if missing else "")
    return (False, f"未知缺陷系统：{tracker}")
"""缺陷联动（atf.bugfiler）：自动化测试失败 → 禅道 / Jira 缺陷。

模块边界（一条直线，谁也别跨界）：

    BugDraft（models）      只描述"这次失败是什么"，不含任何 HTTP 概念
        ↓
    DedupeStore（dedupe）   指纹 → 已提单记录，回答"提过没有"
        ↓
    ZenTaoClient / JiraClient   只负责把 BugDraft 翻译成某系统的请求与响应
        ↓
    mock_server（测试夹具）  用本地端点固化协议细节，让上面两层可被端到端验证

对外只需要这一处导入面：

    from atf.bugfiler import BugDraft, BugFilerError, DedupeStore, ZenTaoClient, JiraClient

mock_server 刻意**不做**这里的一等导出：它 import 了 http.server，属于测试/调试设施，
主流程不该顺带把它拉进来（导入面越小，越不容易让 mock 混进生产链路）。
"""

from __future__ import annotations

from .dedupe import DEFAULT_STATE_PATH, DedupeStore
from .jira import JiraClient
from .models import SEVERITIES, BugDraft, BugFilerError, fingerprint_for, mask_secret, normalize_actual
from .zentao import ZenTaoClient

__all__ = [
    "BugDraft",
    "BugFilerError",
    "DedupeStore",
    "DEFAULT_STATE_PATH",
    "JiraClient",
    "SEVERITIES",
    "ZenTaoClient",
    "fingerprint_for",
    "mask_secret",
    "normalize_actual",
]

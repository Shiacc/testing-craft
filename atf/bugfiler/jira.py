"""Jira Cloud REST 客户端：一次失败 → 一张 Jira 缺陷单。

为什么描述要用 ADF（Atlassian Document Format）而不是 Markdown 字符串：
Jira Cloud 的 ``/rest/api/3/issue`` 只接受富文本 JSON 文档（REST API v3 起），传纯
Markdown 会得到 400；而 v2 虽能收纯文本，却无法换行分块、代码块也不等高亮。这里只用
``paragraph`` 与 ``codeBlock`` 两种最小节点——够表达"定位信息 / 期望 / 实际堆栈"，
又不需要维护一整套富文本构造器。

为什么认证用 Basic Auth 而不是自己拼 header：
``email:api_token`` 的 base64 由 httpx 的 ``BasicAuth`` 生成，既省掉一处手写 base64 的
出错面，也让 token 只存在于 auth 对象里，不进入任何日志字符串。

为什么优先级要翻译：Jira 的 priority 是按项目配置的**名字**（默认 Highest/High/Medium/Low），
而 BugDraft 用的是 1-4 整数（跨系统统一口径）。翻译放在 adapter 里，中间层保持中立。
"""

from __future__ import annotations

import httpx

from .models import BugDraft, BugFilerError, mask_secret

ISSUE_PATH = "/rest/api/3/issue"
# BugDraft.priority（1 最紧急）→ Jira 默认优先级方案。
JIRA_PRIORITY: dict[int, str] = {1: "Highest", 2: "High", 3: "Medium", 4: "Low"}
SNIPPET_LIMIT = 240


class JiraClient:
    """Jira Cloud 缺陷客户端。project_key 与 issue_type 决定单子落在哪个项目。"""

    def __init__(
        self,
        base_url: str,
        *,
        email: str,
        api_token: str,
        project_key: str,
        issue_type: str = "Bug",
        timeout: float = 10.0,
    ) -> None:
        base = (base_url or "").strip().rstrip("/")
        if not base:
            raise BugFilerError("JiraClient.base_url 不能为空（如 https://example.atlassian.net）")
        self._email = (email or "").strip()
        self._api_token = (api_token or "").strip()
        self.project_key = (project_key or "").strip().upper()
        self.issue_type = (issue_type or "").strip() or "Bug"
        self.base_url = base
        self.timeout = float(timeout)

        if not self._email:
            raise BugFilerError("JiraClient.email 不能为空（Basic Auth 需要账号邮箱）")
        if not self._api_token:
            raise BugFilerError("JiraClient.api_token 不能为空（Atlassian API Token）")
        if not self.project_key:
            raise BugFilerError("JiraClient.project_key 不能为空（如 QA）")

    # ------------------------------------------------------------ 公开接口

    @property
    def token_hint(self) -> str:
        """可安全打印的 token 形态。"""
        return mask_secret(self._api_token)

    def create(self, bug: BugDraft) -> dict:
        """提交一张缺陷单，返回 ``{"tracker", "key", "url", "raw"}``。"""
        if not isinstance(bug, BugDraft):
            raise BugFilerError("JiraClient.create 需要 BugDraft 实例")
        payload = {"fields": self._fields(bug)}
        with httpx.Client(base_url=self.base_url, auth=httpx.BasicAuth(self._email, self._api_token)) as client:
            try:
                response = client.post(
                    ISSUE_PATH,
                    json=payload,
                    headers={"Content-Type": "application/json", "Accept": "application/json"},
                    timeout=self.timeout,
                )
            except httpx.HTTPError as exc:
                kind = "超时" if isinstance(exc, httpx.TimeoutException) else "网络错误"
                raise BugFilerError(
                    f"Jira 请求{kind}（{self.timeout:g}s，{ISSUE_PATH}）：{self._safe(str(exc))}"
                ) from exc

        if response.status_code >= 400:
            snippet = self._snippet_text(response.text)
            raise BugFilerError(
                f"Jira 返回 {response.status_code}{self._auth_hint(response.status_code)}："
                f"{snippet or '(空响应)'}",
                status_code=response.status_code,
                snippet=snippet,
            )
        try:
            raw = response.json()
        except ValueError as exc:
            snippet = self._snippet_text(response.text)
            raise BugFilerError(
                f"Jira 返回非 JSON（{response.status_code}）：{snippet or '(空响应)'}",
                status_code=response.status_code,
                snippet=snippet,
            ) from exc
        if not isinstance(raw, dict):
            raise BugFilerError(
                f"Jira 响应结构异常（期望对象，得到 {type(raw).__name__}）",
                status_code=response.status_code,
            )
        key = str(raw.get("key") or "").strip()
        if not key:
            raise BugFilerError(
                f"Jira 创建缺陷成功但响应里没有 key：{self._snippet_text(str(raw))}",
                status_code=response.status_code,
            )
        return {"tracker": "jira", "key": key, "url": f"{self.base_url}/browse/{key}", "raw": raw}

    # ------------------------------------------------------------ 载荷

    def _fields(self, bug: BugDraft) -> dict:
        fields: dict = {
            "project": {"key": self.project_key},
            "summary": bug.title,
            "issuetype": {"name": self.issue_type},
            "priority": {"name": JIRA_PRIORITY[bug.priority]},
            "description": self._adf(bug),
        }
        if bug.environment:
            fields["environment"] = bug.environment
        if bug.labels:
            fields["labels"] = list(bug.labels)
        return fields

    @staticmethod
    def _adf(bug: BugDraft) -> dict:
        """最小 ADF：paragraph 承载文字，codeBlock 承载步骤与堆栈。

        指纹也写进正文：Jira 侧没有自定义字段可放（每个项目的字段 id 都不一样），
        放在正文里就能用 JQL 全文检索回查"这条单对应哪次自动生成"。
        """
        content: list[dict] = [JiraClient._paragraph(bug.trace_text())]
        content.extend(JiraClient._paragraphs(bug.description))
        content.append(JiraClient._paragraph("复现步骤"))
        content.append(JiraClient._code_block(bug.steps_text()))
        content.extend(JiraClient._paragraphs(bug.replay_text()))
        content.append(JiraClient._paragraph(f"指纹：{bug.fingerprint}"))
        return {"type": "doc", "version": 1, "content": content}

    # ------------------------------------------------------------ ADF 节点

    @staticmethod
    def _text(value: str) -> dict:
        return {"type": "text", "text": str(value)}

    @classmethod
    def _paragraph(cls, value: str) -> dict:
        """段落内多行用 hardBreak 连接：ADF 里换行必须是显式节点，``\\n`` 会被吞掉。"""
        lines = [line for line in str(value or "").splitlines()] or [""]
        nodes: list[dict] = []
        for index, line in enumerate(lines):
            if index:
                nodes.append({"type": "hardBreak"})
            if line:
                nodes.append(cls._text(line))
        if not nodes:
            nodes.append(cls._text(" "))
        return {"type": "paragraph", "content": nodes}

    @classmethod
    def _paragraphs(cls, value: str) -> list[dict]:
        """按空行切段：一段一个 paragraph，读起来接近原文的段落结构。"""
        blocks = [block.strip() for block in str(value or "").split("\n\n") if block.strip()]
        return [cls._paragraph(block) for block in blocks]

    @classmethod
    def _code_block(cls, value: str) -> dict:
        return {
            "type": "codeBlock",
            "attrs": {"language": "text"},
            "content": [cls._text(str(value or "").strip() or "(空)")],
        }

    # ------------------------------------------------------------ 错误与脱敏

    @staticmethod
    def _auth_hint(status: int) -> str:
        if status in (401, 403):
            return "（认证失败：检查 email/api_token，以及该账号是否有项目创建权限）"
        if status == 400:
            return "（请求被拒：通常是 project_key / issuetype 名称在当前项目里不存在）"
        return ""

    def _safe(self, text: str) -> str:
        out = str(text)
        for secret in (self._api_token, self._email):
            if secret:
                out = out.replace(secret, mask_secret(secret))
        return out

    def _snippet_text(self, text: str) -> str:
        return self._safe(" ".join(str(text or "").split()))[:SNIPPET_LIMIT]

    def __repr__(self) -> str:  # 任何日志里都不出现明文 email 之外的凭据
        return (
            f"JiraClient(base_url={self.base_url!r}, project_key={self.project_key!r}, "
            f"issue_type={self.issue_type!r}, api_token={self.token_hint})"
        )

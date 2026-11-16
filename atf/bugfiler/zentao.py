"""禅道（ZenTao）REST 客户端：一次失败 → 一张禅道缺陷单。

为什么认证要写成"token 优先、否则账号密码换 token"：
禅道 v1 API 的路径要求请求头带 ``Token``。人工调试时手边通常只有账号密码，而 CI 里
应该固化一个短期 token（避免把密码写进流水线变量）。两种入口都在客户端内部收敛成
同一个 ``Token`` 头，调用方不需要知道当前用的是哪条路。

为什么 ``severity`` 发数字而不是中文：
禅道字段口径是 1-4（1 最严重），中文四档只在**人读**的层面存在。这里做人读↔机读的
翻译：body 里的 ``severity`` 是数字，``steps`` 文本里同时写上"严重程度：严重"，保证
缺陷单打开就能被人看懂，而接口不会因为中文枚举被拒。

为什么打开失败的响应要做成 ``BugFilerError`` 并带状态码 + 响应片段：
自动化提单最典型的失败是 401（token 过期）和 400（字段被产品配置校验掉）。
只抛"请求失败"，使用者只能去翻 CI 日志里的整段 traceback；带上状态码与截断片段，
异常本身就能说明问题，而且片段经过脱敏，可以安全地打到日志里。
"""

from __future__ import annotations

import httpx

from .models import BugDraft, BugFilerError, mask_secret

# 禅道 severity：1 致命 / 2 严重 / 3 一般 / 4 轻微（与 BugDraft 的中文档一一对应）。
ZENTAO_SEVERITY: dict[str, int] = {"致命": 1, "严重": 2, "一般": 3, "轻微": 4}
TOKEN_PATH = "/api.php/v1/tokens"
BUGS_PATH = "/api.php/v1/bugs"
SNIPPET_LIMIT = 240


class ZenTaoClient:
    """禅道缺陷客户端。构造即校验参数，避免 CI 里跑到最后一刻才发现没配产品。"""

    def __init__(
        self,
        base_url: str,
        *,
        product_id: int,
        token: str = "",
        account: str = "",
        password: str = "",
        timeout: float = 10.0,
    ) -> None:
        base = (base_url or "").strip().rstrip("/")
        if not base:
            raise BugFilerError("ZenTaoClient.base_url 不能为空（如 https://zentao.example.com）")
        try:
            product = int(product_id)
        except (TypeError, ValueError) as exc:
            raise BugFilerError(f"ZenTaoClient.product_id 必须是整数，收到 {product_id!r}") from exc
        if product <= 0:
            raise BugFilerError("ZenTaoClient.product_id 必须是正整数")

        self._token = (token or "").strip()
        self.account = (account or "").strip()
        self._password = (password or "").strip()
        if not self._token and not (self.account and self._password):
            raise BugFilerError("ZenTaoClient 需要 token，或同时提供 account 与 password")

        self.base_url = base
        self.product_id = product
        self.timeout = float(timeout)

    # ------------------------------------------------------------ 公开接口

    @property
    def token_hint(self) -> str:
        """可安全打印的 token 形态（用于启动日志确认"到底配没配上"）。"""
        return mask_secret(self._token)

    def create(self, bug: BugDraft) -> dict:
        """提交一张缺陷单，返回 ``{"tracker", "key", "url", "raw"}``。

        key 用禅道缺陷 id 的字符串形式；url 用禅道网页端 ``bug-view-<id>.html`` 路由。
        """
        if not isinstance(bug, BugDraft):
            raise BugFilerError("ZenTaoClient.create 需要 BugDraft 实例")
        payload = self._build_payload(bug)
        with httpx.Client(base_url=self.base_url) as client:
            token = self._resolve_token(client)
            raw = self._post(client, BUGS_PATH, payload, token=token)
        key = str(raw.get("id") or raw.get("key") or "").strip()
        if not key:
            raise BugFilerError(
                f"禅道创建缺陷成功但响应里没有 id/key：{self._snippet(raw)}",
                status_code=200,
                snippet=self._snippet(raw),
            )
        return {"tracker": "zentao", "key": key, "url": self._bug_url(key), "raw": raw}

    def _bug_url(self, key: str) -> str:
        return f"{self.base_url}/bug-view-{key}.html"

    # ------------------------------------------------------------ 认证

    def _resolve_token(self, client: httpx.Client) -> str:
        """已有 token 直接返回；否则用账号密码换一个并缓存，避免每张单都换一次。"""
        if self._token:
            return self._token
        body = {"account": self.account, "password": self._password}
        raw = self._post(client, TOKEN_PATH, body, token="", login=True)
        token = str(raw.get("token") or "").strip()
        if not token:
            raise BugFilerError(f"禅道登录成功但未返回 token：{self._snippet(raw)}")
        self._token = token
        return token

    # ------------------------------------------------------------ HTTP

    def _post(
        self,
        client: httpx.Client,
        path: str,
        payload: dict,
        *,
        token: str,
        login: bool = False,
    ) -> dict:
        """所有出站请求的唯一入口：统一超时、统一错误包装、统一脱敏。

        ``login=True`` 只影响 401 时的提示语：换 token 的请求失败，说明账号密码不对；
        提单请求失败，才轮到"token 过期 / 没有该产品权限"。
        """
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if token:
            headers["Token"] = token
        try:
            response = client.post(path, json=payload, headers=headers, timeout=self.timeout)
        except httpx.HTTPError as exc:  # 含超时、连接失败、DNS 失败
            kind = "超时" if isinstance(exc, httpx.TimeoutException) else "网络错误"
            raise BugFilerError(
                f"禅道请求{kind}（{self.timeout:g}s，{path}）：{self._safe(str(exc))}"
            ) from exc

        if response.status_code >= 400:
            snippet = self._snippet_text(response.text)
            hint = self._auth_hint(response.status_code, login=login)
            raise BugFilerError(
                f"禅道返回 {response.status_code}{hint}：{snippet or '(空响应)'}",
                status_code=response.status_code,
                snippet=snippet,
            )
        try:
            body = response.json()
        except ValueError as exc:
            snippet = self._snippet_text(response.text)
            raise BugFilerError(
                f"禅道返回非 JSON（{response.status_code}）：{snippet or '(空响应)'}",
                status_code=response.status_code,
                snippet=snippet,
            ) from exc
        if not isinstance(body, dict):
            raise BugFilerError(
                f"禅道响应结构异常（期望对象，得到 {type(body).__name__}）：{self._snippet(body)}",
                status_code=response.status_code,
            )
        return body

    @staticmethod
    def _auth_hint(status: int, *, login: bool = False) -> str:
        if status in (401, 403):
            if login:
                return "（认证失败：account/password 被拒绝，检查账号、密码与产品权限）"
            return "（认证失败：检查 token 是否过期，或 account/password 是否有该产品的提单权限）"
        if status == 404:
            return "（端点不存在：确认 base_url 是否指向禅道根路径，以及是否开启了 REST API）"
        return ""

    # ------------------------------------------------------------ 载荷

    def _build_payload(self, bug: BugDraft) -> dict:
        return {
            "product": self.product_id,
            "title": bug.title,
            "severity": ZENTAO_SEVERITY[bug.severity],
            "pri": bug.priority,
            "type": "codeerror",  # 自动化失败在禅道侧一律归"代码错误"，便于统计自动化噪声
            "openedBy": self.account or "atf-bugfiler",
            "steps": self._steps_text(bug),
        }

    @staticmethod
    def _steps_text(bug: BugDraft) -> str:
        """禅道 steps 是单段富文本字段，多行拼装最稳，不依赖 HTML 渲染。"""
        # trace_text 用"；"分隔各定位信息，这里换成禅道习惯的【】标签形式。
        trace = "【" + bug.trace_text().replace("；", "】【") + "】" if bug.trace_text() else ""
        blocks = [
            "【来源】自动化测试失败（atf.bugfiler 自动提交）",
            trace,
            f"【复现步骤】\n{bug.steps_text()}",
            bug.replay_text(),
            f"【指纹】{bug.fingerprint}",
        ]
        return "\n\n".join(block for block in blocks if block.strip())

    # ------------------------------------------------------------ 脱敏

    def _safe(self, text: str) -> str:
        """把可能出现在异常文本里的 token / 密码抹掉。"""
        out = str(text)
        for secret in (self._token, self._password):
            if secret:
                out = out.replace(secret, mask_secret(secret))
        return out

    def _snippet_text(self, text: str) -> str:
        return self._safe(" ".join(str(text or "").split()))[:SNIPPET_LIMIT]

    @classmethod
    def _snippet(cls, body: object) -> str:
        return " ".join(str(body).split())[:SNIPPET_LIMIT]

    def __repr__(self) -> str:  # 任何日志里都不出现明文密码/token
        return (
            f"ZenTaoClient(base_url={self.base_url!r}, product_id={self.product_id}, "
            f"token={self.token_hint}, account={self.account or '(未设置)'})"
        )

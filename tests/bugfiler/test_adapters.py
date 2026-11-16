"""缺陷联动适配器的端到端测试：对着本地 mock 缺陷服务跑真实 HTTP。

为什么这些测试要起真服务，而不是 monkeypatch ``httpx.Client.post``：
这层代码的全部职责就是"**发出一个正确的 HTTP 请求**"——认证头、字段名、JSON 结构、
状态码分支。mock 掉 HTTP 层，等于把要验证的东西一起 mock 掉了（还能顺手"通过"一堆
字段写错的实现）。起一个真监听 127.0.0.1 的 mock 服务，请求会真的被序列化、发出、
解析，测试断言的是**对方实际收到了什么**。

为什么失败路径（4xx）也要测：真实环境最常见的就是 token 过期与字段被拒。若只测 happy
path，"错误信息带状态码、且不泄露凭据"这条约定就永远是没人验证过的承诺。
"""

from __future__ import annotations

import base64

import pytest

from atf.bugfiler import BugDraft, BugFilerError, JiraClient, ZenTaoClient
from atf.bugfiler.mock_server import (
    JIRA_ISSUE_PATH,
    ZENTAO_BUG_PATH,
    ZENTAO_TOKEN_PATH,
    ThreadedMockServer,
)

# ---------------------------------------------------------------- 夹具


@pytest.fixture()
def mock() -> ThreadedMockServer:
    """每个测试一个独立 mock 服务：端口随机，记录区互不污染。"""
    with ThreadedMockServer() as server:
        yield server


@pytest.fixture()
def bug() -> BugDraft:
    return BugDraft(
        title="结算页满减券未生效",
        description="金卡会员提交订单时，满 100 减 20 的优惠券没有从应付金额里扣除。",
        case_id="CAT-001",
        test_id="tests/test_checkout.py::test_coupon_applied",
        severity="严重",
        priority=2,
        environment="prod-readonly",
        steps=["登录金卡会员账号", "加入 100 元商品", "结算页勾选 FULL100-20 并提交"],
        expected="应付金额 90.00",
        actual="AssertionError: payable 100.00 != 90.00",
        labels=["automated", "checkout"],
    )


def _collect_text(node: object) -> str:
    """把 ADF 文档里所有 text 节点的文字拼起来，便于做"内容真的进去了"的断言。"""
    if isinstance(node, dict):
        parts = [str(node.get("text", ""))]
        parts.extend(_collect_text(value) for value in node.values())
        return " ".join(parts)
    if isinstance(node, list):
        return " ".join(_collect_text(item) for item in node)
    return ""


# ---------------------------------------------------------------- 禅道


def test_zentao_token_auth_then_create(mock: ThreadedMockServer, bug: BugDraft) -> None:
    """账号密码先换 token，再带 Token 头提单；断言 mock 实际收到的字段。"""
    client = ZenTaoClient(mock.base_url, product_id=7, account="qa_bot", password="s3cret-pw")

    result = client.create(bug)

    assert result["tracker"] == "zentao"
    assert result["key"] == "101"
    assert result["url"] == f"{mock.base_url}/bug-view-101.html"
    assert result["raw"]["title"] == bug.title

    # 1) 认证请求真的发过，且只发了账号密码
    login = mock.last(ZENTAO_TOKEN_PATH)
    assert login is not None
    assert login["body"] == {"account": "qa_bot", "password": "s3cret-pw"}

    # 2) 提单请求带上了换来的 token
    create = mock.last(ZENTAO_BUG_PATH)
    assert create is not None
    assert create["headers"]["token"] == "mock-token"
    assert create["headers"]["content-type"] == "application/json"

    # 3) 关键字段齐全且语义正确
    body = create["body"]
    assert body["product"] == 7
    assert body["title"] == bug.title
    assert body["severity"] == 2  # 严重 → 禅道数字口径
    assert body["pri"] == 2
    assert body["openedBy"] == "qa_bot"
    assert body["steps"].strip()
    assert "1. 登录金卡会员账号" in body["steps"]
    assert "3. 结算页勾选 FULL100-20 并提交" in body["steps"]
    assert bug.fingerprint in body["steps"]  # 指纹写进正文，便于人工回查


def test_zentao_direct_token_skips_login(bug: BugDraft) -> None:
    """直连 token 模式：不该再打 /tokens 端点。"""
    with ThreadedMockServer(token="preset-token") as server:
        client = ZenTaoClient(server.base_url, product_id=7, token="preset-token")

        result = client.create(bug)

        assert result["key"] == "101"
        assert server.find(ZENTAO_TOKEN_PATH) == []
        create = server.last(ZENTAO_BUG_PATH)
        assert create is not None
        assert create["headers"]["token"] == "preset-token"


def test_zentao_4xx_raises_bugfiler_error() -> None:
    """注入 400：必须抛 BugFilerError，消息里带状态码，且不含任何凭据。"""
    with ThreadedMockServer(fail_routes={ZENTAO_BUG_PATH: 400}) as server:
        client = ZenTaoClient(server.base_url, product_id=7, token="very-secret-token")
        draft = BugDraft(title="字段被拒", description="模拟禅道校验失败", actual="boom")

        with pytest.raises(BugFilerError) as excinfo:
            client.create(draft)

    assert excinfo.value.status_code == 400
    assert "400" in str(excinfo.value)
    assert "very-secret-token" not in str(excinfo.value)
    assert excinfo.value.snippet  # 响应片段要留下，否则日志里只剩一句"请求失败"


def test_zentao_login_failure_surfaces_401() -> None:
    """token 过期是最常见的线上故障：401 要能一眼看出是认证问题。"""
    with ThreadedMockServer(fail_routes={ZENTAO_TOKEN_PATH: 401}) as server:
        client = ZenTaoClient(server.base_url, product_id=7, account="qa_bot", password="expired")
        draft = BugDraft(title="认证失败", description="模拟 token 过期", actual="boom")

        with pytest.raises(BugFilerError) as excinfo:
            client.create(draft)

    message = str(excinfo.value)
    assert excinfo.value.status_code == 401
    assert "401" in message
    assert "认证失败" in message
    assert "expired" not in message


def test_zentao_requires_credentials() -> None:
    """构造即校验：没给 token 也没给账号密码时，不该等到发请求才失败。"""
    with pytest.raises(BugFilerError) as excinfo:
        ZenTaoClient("http://127.0.0.1:1", product_id=7)

    assert "token" in str(excinfo.value)


# ---------------------------------------------------------------- Jira


def test_jira_basic_auth_creates_issue_with_adf(mock: ThreadedMockServer, bug: BugDraft) -> None:
    """Jira：Basic Auth + fields 结构 + ADF 描述。"""
    client = JiraClient(
        mock.base_url, email="qa@example.com", api_token="jira-token", project_key="qa"
    )

    result = client.create(bug)

    assert result["tracker"] == "jira"
    assert result["key"] == "QA-1"
    assert result["url"] == f"{mock.base_url}/browse/QA-1"

    record = mock.last(JIRA_ISSUE_PATH)
    assert record is not None

    # 认证：Basic base64(email:api_token)
    scheme, _, payload = record["headers"]["authorization"].partition(" ")
    assert scheme.lower() == "basic"
    assert base64.b64decode(payload).decode("utf-8") == "qa@example.com:jira-token"

    fields = record["body"]["fields"]
    assert fields["project"]["key"] == "QA"  # project_key 统一大写
    assert fields["summary"] == bug.title
    assert fields["issuetype"]["name"] == "Bug"
    assert fields["priority"]["name"] == "High"  # priority=2
    assert fields["labels"] == ["automated", "checkout"]
    assert fields["environment"] == "prod-readonly"

    # 描述必须是 ADF 文档，且同时含 paragraph 与 codeBlock
    description = fields["description"]
    assert description["type"] == "doc"
    assert description["version"] == 1
    kinds = {node["type"] for node in description["content"]}
    assert {"paragraph", "codeBlock"} <= kinds

    text = _collect_text(description)
    assert "CAT-001" in text
    assert "tests/test_checkout.py::test_coupon_applied" in text
    assert "1. 登录金卡会员账号" in text
    assert "payable 100.00 != 90.00" in text
    assert bug.fingerprint in text


@pytest.mark.parametrize(("priority", "expected"), [(1, "Highest"), (2, "High"), (3, "Medium"), (4, "Low")])
def test_jira_priority_mapping(mock: ThreadedMockServer, priority: int, expected: str) -> None:
    """优先级 1-4 必须映射到 Jira 默认方案的四个名字。"""
    client = JiraClient(mock.base_url, email="qa@example.com", api_token="t", project_key="QA")
    draft = BugDraft(title="优先级映射", description="验证 1-4 → Highest/High/Medium/Low", priority=priority)

    client.create(draft)

    record = mock.last(JIRA_ISSUE_PATH)
    assert record is not None
    assert record["body"]["fields"]["priority"]["name"] == expected


def test_jira_4xx_raises_with_hint() -> None:
    """Jira 400 通常意味着 project/issuetype 配错，异常里要给出这条提示。"""
    with ThreadedMockServer(fail_routes={JIRA_ISSUE_PATH: 400}) as server:
        client = JiraClient(server.base_url, email="qa@example.com", api_token="tok-123456", project_key="QA")
        draft = BugDraft(title="项目配置错误", description="模拟 Jira 拒绝", actual="boom")

        with pytest.raises(BugFilerError) as excinfo:
            client.create(draft)

    assert excinfo.value.status_code == 400
    assert "400" in str(excinfo.value)
    assert "project_key" in str(excinfo.value)
    assert "tok-123456" not in str(excinfo.value)


def test_jira_requires_api_token() -> None:
    """构造即校验：缺 api_token 时立刻失败，而不是发一个必被 401 的请求。"""
    with pytest.raises(BugFilerError) as excinfo:
        JiraClient("http://127.0.0.1:1", email="qa@example.com", api_token="", project_key="QA")

    assert "api_token" in str(excinfo.value)


def test_repr_does_not_leak_secrets(mock: ThreadedMockServer) -> None:
    """客户端被 print 到 CI 日志时不出现明文凭据。"""
    zentao = ZenTaoClient(mock.base_url, product_id=1, token="very-secret-token")
    jira = JiraClient(mock.base_url, email="qa@example.com", api_token="jira-secret-token", project_key="QA")

    assert "very-secret-token" not in repr(zentao)
    assert "jira-secret-token" not in repr(jira)
    assert "***" in repr(zentao) and "***" in repr(jira)

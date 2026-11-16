"""契约测试：锁住"对外承诺"，而不是锁住实现。

与普通接口测试的区别：
- 普通接口测试验证「行为是否正确」；
- 契约测试验证「对外形状是否兼容」——字段被删、类型被改、状态码被改，都会让下游炸掉，
  但这类破坏往往在"业务逻辑仍然正确"的前提下发生，功能测试抓不到。

这里用 OpenAPI 作为契约源（真实项目中消费方驱动的 Pact 更严格，见 docs/03 §6）。
"""

from __future__ import annotations

import pytest

EXPECTED_PATHS = {
    "/health",
    "/api/quote",
    "/api/orders",
    "/api/orders/{order_id}",
    "/api/orders/{order_id}/pay",
}

ORDER_FIELDS = {"order_id", "status", "subtotal", "discount", "payable", "created_at"}
QUOTE_FIELDS = {"subtotal", "discount", "payable"}


def _schema_of(app, path: str, method: str, status_code: str) -> dict:
    return app.openapi()["paths"][path][method]["responses"][status_code]["content"]["application/json"]["schema"]


def _resolved_properties(app, schema: dict) -> dict:
    """解析 $ref（Pydantic 生成的组件引用），拿到真正的 properties。"""
    if "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[-1]
        return app.openapi()["components"]["schemas"][name]["properties"]
    return schema.get("properties", {})


def test_openapi_paths_are_stable(client):
    """接口清单只允许"增"，不允许悄悄"减"。新增在这里补一行即可，删除会立刻失败。"""
    import app.main as main

    actual = set(main.app.openapi()["paths"].keys())
    assert actual == EXPECTED_PATHS


def test_quote_response_fields_contract(client, alice, order_payload):
    import app.main as main

    schema = _schema_of(main.app, "/api/quote", "post", "200")
    properties = _resolved_properties(main.app, schema)
    assert set(properties.keys()) == QUOTE_FIELDS

    body = client.post("/api/quote", json=order_payload(), headers=alice).json()
    assert set(body.keys()) == QUOTE_FIELDS
    for field in QUOTE_FIELDS:
        assert isinstance(body[field], str), f"{field} 必须是字符串金额，避免浮点精度问题"


def test_order_response_fields_contract(client, alice, order_payload):
    import app.main as main

    schema = _schema_of(main.app, "/api/orders", "post", "201")
    properties = _resolved_properties(main.app, schema)
    assert set(properties.keys()) == ORDER_FIELDS

    body = client.post(
        "/api/orders", json=order_payload(), headers={**alice, "Idempotency-Key": "key-contract"}
    ).json()
    assert set(body.keys()) == ORDER_FIELDS
    assert body["status"] in {"CREATED", "PAID"}


@pytest.mark.parametrize(
    ("path", "method", "status_code"),
    [
        ("/api/quote", "post", "401"),
        ("/api/orders", "post", "400"),
        ("/api/orders", "post", "409"),
        ("/api/orders/{order_id}", "get", "404"),
    ],
)
def test_error_responses_are_documented(client, path, method, status_code):
    """错误码必须出现在契约里：调用方靠它做分支，未文档化的错误码等于线上惊喜。"""
    import app.main as main

    responses = main.app.openapi()["paths"][path][method]["responses"]
    assert status_code in responses


def test_error_body_shape_is_uniform(client, order_payload):
    """"错误体结构一致性"是契约的一部分：任何接口的错误都应该有 code 与 message。"""
    unauth = client.post("/api/quote", json=order_payload()).json()
    invalid = client.post(
        "/api/quote", json={"items": [{"sku": "S", "unit_price": "1.23", "quantity": 0}]}, headers={"Authorization": "Bearer tok-alice"}
    ).json()

    for body in (unauth, invalid):
        assert set(body) >= {"code", "message"}
        assert isinstance(body["code"], str) and body["code"].isupper()
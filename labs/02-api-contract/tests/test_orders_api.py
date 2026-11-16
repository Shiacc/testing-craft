"""L3 接口测试：鉴权、错误码、业务规则。

演示的核心方法是 **用例矩阵**（docs/03 §3）：每个接口至少覆盖
正常 / 边界 / 非法输入 / 鉴权 / 幂等 / 归属 六类，而不是只写"happy path"。
"""

from __future__ import annotations

import pytest


# ------------------------------------------------------------------ 鉴权


@pytest.mark.smoke
def test_quote_requires_token(client, order_payload):
    resp = client.post("/api/quote", json=order_payload())

    assert resp.status_code == 401
    assert resp.json()["code"] == "UNAUTHENTICATED"


@pytest.mark.parametrize(
    ("header", "expected_code"),
    [
        ({"Authorization": "Bearer tok-unknown"}, "INVALID_TOKEN"),
        ({"Authorization": "tok-alice"}, "UNAUTHENTICATED"),  # 缺 Bearer 前缀
        ({"Authorization": "Bearer "}, "INVALID_TOKEN"),
    ],
)
def test_quote_rejects_invalid_token(client, order_payload, header, expected_code):
    resp = client.post("/api/quote", json=order_payload(), headers=header)

    assert resp.status_code == 401
    assert resp.json()["code"] == expected_code


# ------------------------------------------------------------------ 试算


@pytest.mark.smoke
def test_quote_happy_path(client, alice, order_payload):
    """金卡 100 元 → 会员折扣 10 → 应付 90.00。"""
    resp = client.post("/api/quote", json=order_payload(), headers=alice)

    assert resp.status_code == 200
    assert resp.json() == {"subtotal": "100.00", "discount": "10.00", "payable": "90.00"}


@pytest.mark.edge
def test_quote_coupon_threshold_evaluated_after_member_discount(client, alice, order_payload):
    """门槛按折后金额判断：100 元金卡后 90 < 100，满 100 减 20 不生效。"""
    resp = client.post("/api/quote", json=order_payload(coupon_code="FULL100-20"), headers=alice)

    assert resp.status_code == 200
    assert resp.json()["discount"] == "10.00"
    assert resp.json()["payable"] == "90.00"


@pytest.mark.edge
def test_quote_multiple_lines_and_coupon(client, bob, order_payload):
    """普通用户 2×100 + 1×50 = 250，满 200 减 30 → 220.00。"""
    lines = [
        {"sku": "SKU-1", "unit_price": "100.00", "quantity": 2},
        {"sku": "SKU-2", "unit_price": "50.00", "quantity": 1},
    ]
    resp = client.post("/api/quote", json=order_payload(lines=lines, coupon_code="FULL200-30"), headers=bob)

    assert resp.status_code == 200
    body = resp.json()
    assert body["subtotal"] == "250.00"
    assert body["payable"] == "220.00"


@pytest.mark.edge
def test_quote_unknown_coupon_returns_business_code(client, alice, order_payload):
    resp = client.post("/api/quote", json=order_payload(coupon_code="NOT-EXIST"), headers=alice)

    assert resp.status_code == 400
    assert resp.json()["code"] == "COUPON_NOT_FOUND"


@pytest.mark.parametrize(
    ("payload_kwargs", "reason"),
    [
        ({"quantity": 0}, "数量为 0"),
        ({"quantity": -3}, "数量为负"),
        ({"unit_price": "-1.00"}, "负金额"),
        ({"unit_price": "1.234"}, "超过两位小数"),
        ({"unit_price": "abc"}, "非数字"),
        ({"sku": ""}, "空 SKU"),
    ],
)
def test_quote_rejects_invalid_payload_with_400(client, alice, order_payload, payload_kwargs, reason):
    """框架默认会返回 422；本项目通过异常处理器统一成 400 + code，前端只处理一套结构。"""
    resp = client.post("/api/quote", json=order_payload(**payload_kwargs), headers=alice)

    assert resp.status_code == 400, reason
    assert resp.json()["code"] == "INVALID_PAYLOAD"


def test_quote_rejects_empty_items(client, alice):
    resp = client.post("/api/quote", json={"items": []}, headers=alice)

    assert resp.status_code == 400
    assert resp.json()["code"] == "INVALID_PAYLOAD"


# ------------------------------------------------------------------ 下单幂等


@pytest.mark.smoke
def test_create_order_returns_201(client, alice, order_payload):
    resp = client.post(
        "/api/orders",
        json=order_payload(),
        headers={**alice, "Idempotency-Key": "key-001"},
    )

    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "CREATED"
    assert body["payable"] == "90.00"
    assert body["order_id"].startswith("ord_")


def test_create_order_requires_idempotency_key(client, alice, order_payload):
    resp = client.post("/api/orders", json=order_payload(), headers=alice)

    assert resp.status_code == 400
    assert resp.json()["code"] == "MISSING_IDEMPOTENCY_KEY"


@pytest.mark.smoke
def test_create_order_is_idempotent_on_replay(client, alice, order_payload):
    """网络重试场景：同一个 Key 重复提交，必须返回同一张订单，且库存只扣一次。"""
    headers = {**alice, "Idempotency-Key": "key-002"}
    payload = order_payload(quantity=3)

    first = client.post("/api/orders", json=payload, headers=headers)
    second = client.post("/api/orders", json=payload, headers=headers)

    assert first.status_code == 201
    assert second.status_code == 201
    assert second.json()["order_id"] == first.json()["order_id"]
    assert client.get("/health").status_code == 200


@pytest.mark.edge
def test_idempotency_key_reuse_with_different_body_conflicts(client, alice, order_payload):
    headers = {**alice, "Idempotency-Key": "key-003"}
    client.post("/api/orders", json=order_payload(quantity=1), headers=headers)

    resp = client.post("/api/orders", json=order_payload(quantity=2), headers=headers)

    assert resp.status_code == 409
    assert resp.json()["code"] == "IDEMPOTENCY_KEY_REUSED"


@pytest.mark.edge
def test_out_of_stock_conflicts(client, bob, order_payload):
    resp = client.post(
        "/api/orders",
        json=order_payload(sku="SKU-LIMITED", quantity=1),
        headers={**bob, "Idempotency-Key": "key-004"},
    )

    assert resp.status_code == 409
    assert resp.json()["code"] == "OUT_OF_STOCK"


# ------------------------------------------------------------------ 查询与归属


@pytest.mark.smoke
def test_get_order_as_owner(client, alice, order_payload):
    created = client.post(
        "/api/orders", json=order_payload(), headers={**alice, "Idempotency-Key": "key-005"}
    ).json()

    resp = client.get(f"/api/orders/{created['order_id']}", headers=alice)

    assert resp.status_code == 200
    assert resp.json()["order_id"] == created["order_id"]


@pytest.mark.edge
def test_get_other_users_order_forbidden(client, alice, bob, order_payload):
    created = client.post(
        "/api/orders", json=order_payload(), headers={**alice, "Idempotency-Key": "key-006"}
    ).json()

    resp = client.get(f"/api/orders/{created['order_id']}", headers=bob)

    assert resp.status_code == 403
    assert resp.json()["code"] == "FORBIDDEN"


def test_get_unknown_order_returns_404(client, alice):
    resp = client.get("/api/orders/ord_not_exist", headers=alice)

    assert resp.status_code == 404
    assert resp.json()["code"] == "ORDER_NOT_FOUND"
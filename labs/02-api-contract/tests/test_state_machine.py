"""状态机测试：用"迁移矩阵"而不是"逐个接口点一遍"。

订单状态只有 CREATED / PAID 两个，但真实系统往往有 6-10 个状态。
把「当前状态 × 动作 → 允许/拒绝」列成矩阵，用例数量线性增长、覆盖却是全覆盖。
"""

from __future__ import annotations

import pytest


def _create(client, headers, order_payload, key: str) -> dict:
    resp = client.post("/api/orders", json=order_payload(), headers={**headers, "Idempotency-Key": key})
    assert resp.status_code == 201
    return resp.json()


@pytest.mark.smoke
def test_created_to_paid(client, alice, order_payload):
    order = _create(client, alice, order_payload, "sm-1")

    resp = client.post(f"/api/orders/{order['order_id']}/pay", headers=alice)

    assert resp.status_code == 200
    assert resp.json()["status"] == "PAID"


@pytest.mark.edge
def test_paid_to_paid_rejected(client, alice, order_payload):
    """重复支付必须被拒绝，而不是静默成功（否则会重复扣款）。"""
    order = _create(client, alice, order_payload, "sm-2")
    client.post(f"/api/orders/{order['order_id']}/pay", headers=alice)

    resp = client.post(f"/api/orders/{order['order_id']}/pay", headers=alice)

    assert resp.status_code == 409
    assert resp.json()["code"] == "ORDER_ALREADY_PAID"


@pytest.mark.edge
def test_pay_others_order_forbidden(client, alice, bob, order_payload):
    order = _create(client, alice, order_payload, "sm-3")

    resp = client.post(f"/api/orders/{order['order_id']}/pay", headers=bob)

    assert resp.status_code == 403


def test_pay_unknown_order_not_found(client, alice):
    resp = client.post("/api/orders/ord_missing/pay", headers=alice)

    assert resp.status_code == 404
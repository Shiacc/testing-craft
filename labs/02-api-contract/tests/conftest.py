"""公共 fixture：客户端、鉴权头、请求体工厂、以及"每个用例都从干净状态开始"。

第三个是本实验最关键的一条：接口测试最常见的 flaky 来源就是共享可变状态
（订单累积、库存被前面的用例扣光）。这里用 autouse fixture 在用例之间重置，
对应 docs/07 §4 的"数据隔离三原则"。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import main  # noqa: E402


@pytest.fixture()
def client() -> TestClient:
    """基于 httpx 的同步客户端，直接打到 ASGI 应用，无需启动真实服务与端口。"""
    return TestClient(main.app)


@pytest.fixture(autouse=True)
def _reset_state() -> None:
    """用例级重置：订单、幂等键、库存全部回到初始值。"""
    main.ORDERS.clear()
    main.IDEMPOTENCY.clear()
    main.INVENTORY.clear()
    main.INVENTORY.update({"SKU-1": 100, "SKU-2": 5, "SKU-LIMITED": 0})


@pytest.fixture()
def alice() -> dict[str, str]:
    """金卡用户，会员折扣 10%。"""
    return {"Authorization": "Bearer tok-alice"}


@pytest.fixture()
def bob() -> dict[str, str]:
    """普通用户，无会员折扣。"""
    return {"Authorization": "Bearer tok-bob"}


@pytest.fixture()
def order_payload() -> Callable[..., dict[str, Any]]:
    """请求体工厂：默认 2 件 SKU-1（单价 100.00），可按需覆盖。"""

    def _factory(
        sku: str = "SKU-1",
        unit_price: str = "100.00",
        quantity: int = 1,
        coupon_code: str | None = None,
        lines: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "items": lines
            or [{"sku": sku, "unit_price": unit_price, "quantity": quantity}],
        }
        if coupon_code:
            payload["coupon_code"] = coupon_code
        return payload

    return _factory
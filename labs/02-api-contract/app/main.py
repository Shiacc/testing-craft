"""订单服务（Labs 02 的被测对象）。

实验重点不在业务复杂度，而在 **HTTP 契约**：
- 鉴权语义（401 而非 403）、资源归属（403）、资源不存在（404）
- 幂等（Idempotency-Key 重放、同键不同体冲突 409）
- 状态机（CREATED → PAID，非法迁移 409）
- 统一错误体 {"code","message"}，而不是各接口各写一套

说明：真实项目中金额计算应复用 01 实验的领域引擎；此处为保证实验独立可跑，
内联了一份最小实现（金额仍全程使用 Decimal，避免浮点误差）。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

APP_VERSION = "1.0.0"
CENT = Decimal("0.01")

# ------------------------------------------------------------------ 用户与鉴权

TOKENS: dict[str, dict[str, str]] = {
    "tok-alice": {"user_id": "u-alice", "level": "GOLD"},
    "tok-bob": {"user_id": "u-bob", "level": "NORMAL"},
}

MEMBER_RATE = {"NORMAL": "0", "SILVER": "0.05", "GOLD": "0.10", "BLACK": "0.15"}


def current_user(authorization: Annotated[Optional[str], Header()] = None) -> dict[str, str]:
    """缺失或无效凭据一律 401：未认证与认证失败在语义上不可区分。"""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail=("UNAUTHENTICATED", "缺少访问令牌"))
    token = authorization.split(" ", 1)[1].strip()
    user = TOKENS.get(token)
    if user is None:
        raise HTTPException(status_code=401, detail=("INVALID_TOKEN", "访问令牌无效"))
    return user


# ------------------------------------------------------------------ 请求模型


class ItemIn(BaseModel):
    sku: str = Field(min_length=1, max_length=64)
    unit_price: str
    quantity: int = Field(ge=1, le=999)

    @field_validator("unit_price")
    @classmethod
    def _price_must_be_valid(cls, raw: str) -> str:
        try:
            value = Decimal(raw)
        except Exception as exc:  # noqa: BLE001
            raise ValueError("金额格式非法") from exc
        if value < 0:
            raise ValueError("金额不能为负")
        if value.as_tuple().exponent < -2:
            raise ValueError("金额最多两位小数")
        return raw


class QuoteIn(BaseModel):
    items: list[ItemIn] = Field(min_length=1, max_length=100)
    coupon_code: Optional[str] = Field(default=None, max_length=32)


class OrderIn(QuoteIn):
    pass


class OrderOut(BaseModel):
    order_id: str
    status: Literal["CREATED", "PAID"]
    subtotal: str
    discount: str
    payable: str
    created_at: str


class QuoteOut(BaseModel):
    subtotal: str
    discount: str
    payable: str


# ------------------------------------------------------------------ 领域逻辑

COUPONS = {
    "FULL100-20": {"threshold": Decimal("100"), "value": Decimal("20")},
    "FULL200-30": {"threshold": Decimal("200"), "value": Decimal("30")},
}


def _cent(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def calculate(items: list[ItemIn], level: str, coupon_code: Optional[str]) -> tuple[Decimal, Decimal, Decimal]:
    """返回 (小计, 总优惠, 应付)。规则与 01 实验保持一致：先会员折扣，再优惠券。"""
    subtotal = _cent(sum((Decimal(i.unit_price) * i.quantity for i in items), Decimal("0")))
    member = _cent(subtotal * Decimal(MEMBER_RATE[level]))
    after_member = _cent(subtotal - member)

    coupon_value = Decimal("0")
    if coupon_code:
        coupon = COUPONS.get(coupon_code)
        if coupon is None:
            raise HTTPException(status_code=400, detail=("COUPON_NOT_FOUND", f"券不存在: {coupon_code}"))
        if after_member >= coupon["threshold"]:
            coupon_value = _cent(min(coupon["value"], after_member))

    payable = _cent(after_member - coupon_value)
    if payable < 0:
        payable = Decimal("0.00")
    if subtotal > 0 and payable < Decimal("0.01"):
        payable = Decimal("0.01")
    return subtotal, _cent(subtotal - payable), payable


# ------------------------------------------------------------------ 存储

ORDERS: dict[str, dict] = {}
IDEMPOTENCY: dict[tuple[str, str], tuple[str, str]] = {}  # (user, key) -> (order_id, fingerprint)
INVENTORY = {"SKU-1": 100, "SKU-2": 5, "SKU-LIMITED": 0}


def _fingerprint(payload: dict) -> str:
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ------------------------------------------------------------------ 应用

app = FastAPI(
    title="Order Service (Labs 02)",
    version=APP_VERSION,
    description="接口契约测试与幂等实验用的最小订单服务",
)


@app.exception_handler(RequestValidationError)
async def validation_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    """把框架默认的 422 收敛成统一的 400 + 错误体，前端只需处理一套结构。"""
    first = exc.errors()[0] if exc.errors() else {}
    return JSONResponse(
        status_code=400,
        content={"code": "INVALID_PAYLOAD", "message": first.get("msg", "请求体非法"), "path": list(first.get("loc", []))},
    )


@app.exception_handler(HTTPException)
async def http_error_handler(_: Request, exc: HTTPException) -> JSONResponse:
    if isinstance(exc.detail, tuple) and len(exc.detail) == 2:
        code, message = exc.detail
    else:
        code, message = "ERROR", str(exc.detail)
    return JSONResponse(status_code=exc.status_code, content={"code": code, "message": message})


@app.get("/health", summary="健康检查", tags=["ops"])
def health() -> dict[str, str]:
    return {"status": "ok", "version": APP_VERSION}


@app.post(
    "/api/quote",
    response_model=QuoteOut,
    summary="试算（不落单）",
    tags=["order"],
    responses={400: {"description": "请求体非法或券不存在"}, 401: {"description": "未认证"}},
)
def create_quote(body: QuoteIn, user: Annotated[dict, Depends(current_user)]) -> QuoteOut:
    subtotal, discount, payable = calculate(body.items, user["level"], body.coupon_code)
    return QuoteOut(subtotal=str(subtotal), discount=str(discount), payable=str(payable))


@app.post(
    "/api/orders",
    response_model=OrderOut,
    status_code=status.HTTP_201_CREATED,
    summary="下单（幂等）",
    tags=["order"],
    responses={
        400: {"description": "缺少幂等键 / 请求体非法"},
        401: {"description": "未认证"},
        409: {"description": "幂等键冲突或库存不足"},
    },
)
def create_order(
    body: OrderIn,
    user: Annotated[dict, Depends(current_user)],
    idempotency_key: Annotated[Optional[str], Header(alias="Idempotency-Key")] = None,
) -> OrderOut:
    if not idempotency_key:
        raise HTTPException(status_code=400, detail=("MISSING_IDEMPOTENCY_KEY", "下单必须携带 Idempotency-Key"))

    payload = body.model_dump()
    fingerprint = _fingerprint(payload)
    scope = (user["user_id"], idempotency_key)

    if scope in IDEMPOTENCY:
        existing_id, existing_fp = IDEMPOTENCY[scope]
        if existing_fp != fingerprint:
            raise HTTPException(
                status_code=409,
                detail=("IDEMPOTENCY_KEY_REUSED", "同一 Idempotency-Key 不能用于不同的请求体"),
            )
        return OrderOut(**ORDERS[existing_id])

    for item in body.items:
        available = INVENTORY.get(item.sku)
        if available is not None and available < item.quantity:
            raise HTTPException(status_code=409, detail=("OUT_OF_STOCK", f"{item.sku} 库存不足"))

    subtotal, discount, payable = calculate(body.items, user["level"], body.coupon_code)
    order_id = f"ord_{uuid.uuid4().hex[:12]}"
    record = {
        "order_id": order_id,
        "status": "CREATED",
        "subtotal": str(subtotal),
        "discount": str(discount),
        "payable": str(payable),
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "user_id": user["user_id"],
    }
    ORDERS[order_id] = record
    IDEMPOTENCY[scope] = (order_id, fingerprint)

    for item in body.items:
        if item.sku in INVENTORY:
            INVENTORY[item.sku] -= item.quantity

    return OrderOut(**{k: record[k] for k in OrderOut.model_fields})


@app.get(
    "/api/orders/{order_id}",
    response_model=OrderOut,
    summary="查询订单",
    tags=["order"],
    responses={401: {"description": "未认证"}, 403: {"description": "无权访问"}, 404: {"description": "订单不存在"}},
)
def get_order(order_id: str, user: Annotated[dict, Depends(current_user)]) -> OrderOut:
    record = ORDERS.get(order_id)
    if record is None:
        raise HTTPException(status_code=404, detail=("ORDER_NOT_FOUND", "订单不存在"))
    if record["user_id"] != user["user_id"]:
        # 不暴露"存在但不属于你"，但资源级权限仍应明确 403（便于排查越权）
        raise HTTPException(status_code=403, detail=("FORBIDDEN", "无权访问该订单"))
    return OrderOut(**{k: record[k] for k in OrderOut.model_fields})


@app.post(
    "/api/orders/{order_id}/pay",
    response_model=OrderOut,
    summary="支付（状态机）",
    tags=["order"],
    responses={401: {"description": "未认证"}, 403: {"description": "无权操作"}, 404: {"description": "订单不存在"}, 409: {"description": "已支付"}},
)
def pay_order(order_id: str, user: Annotated[dict, Depends(current_user)]) -> OrderOut:
    record = ORDERS.get(order_id)
    if record is None:
        raise HTTPException(status_code=404, detail=("ORDER_NOT_FOUND", "订单不存在"))
    if record["user_id"] != user["user_id"]:
        raise HTTPException(status_code=403, detail=("FORBIDDEN", "无权操作该订单"))
    if record["status"] == "PAID":
        raise HTTPException(status_code=409, detail=("ORDER_ALREADY_PAID", "订单已支付，请勿重复提交"))
    record["status"] = "PAID"
    return OrderOut(**{k: record[k] for k in OrderOut.model_fields})
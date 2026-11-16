"""订单优惠计算引擎（Labs 01 的被测对象）。

业务规则（与 docs/02 的「算钱类逻辑」对应）：
1. 小计 = Σ(单价 × 数量)，单价与金额均为 Decimal，最终按分四舍五入。
2. 会员折扣：普通 0%、银 5%、金 10%、黑金 15%。
3. 优惠券在会员折扣之后使用：
   - 满减券：会员折后金额 ≥ 门槛，减固定金额；
   - 折扣券：会员折后金额 ≥ 门槛，按折扣率计算，可设置优惠上限。
4. 应付金额不得为负数；正价订单最低 1 分。
5. 非法输入（数量 <= 0、单价 < 0、未知会员等级、券参数非法）必须抛 PricingError。

故意保持"纯函数 + 无 IO"，这样 L1 单元测试可以毫秒级覆盖全部分支。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from enum import Enum
from typing import Iterable, Optional

CENT = Decimal("0.01")
MIN_PAYABLE = Decimal("0.01")


class PricingError(ValueError):
    """业务规则不满足时抛出，供上层映射为 400。"""


class MemberLevel(str, Enum):
    NORMAL = "NORMAL"
    SILVER = "SILVER"
    GOLD = "GOLD"
    BLACK = "BLACK"


class CouponKind(str, Enum):
    THRESHOLD = "THRESHOLD"  # 满减
    PERCENT = "PERCENT"  # 折扣


MEMBER_RATE: dict[MemberLevel, Decimal] = {
    MemberLevel.NORMAL: Decimal("0"),
    MemberLevel.SILVER: Decimal("0.05"),
    MemberLevel.GOLD: Decimal("0.10"),
    MemberLevel.BLACK: Decimal("0.15"),
}


def to_cent(value: Decimal) -> Decimal:
    """统一按分四舍五入，避免浮点误差在各处扩散。"""
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class LineItem:
    sku: str
    unit_price: Decimal
    quantity: int

    def __post_init__(self) -> None:
        if not self.sku:
            raise PricingError("sku 不能为空")
        if self.quantity <= 0:
            raise PricingError(f"数量必须为正整数，当前 {self.quantity}")
        if self.unit_price < 0:
            raise PricingError(f"单价不能为负，当前 {self.unit_price}")

    @property
    def amount(self) -> Decimal:
        return to_cent(Decimal(self.unit_price) * self.quantity)


@dataclass(frozen=True)
class Coupon:
    code: str
    kind: CouponKind
    threshold: Decimal
    value: Decimal
    cap: Optional[Decimal] = None

    def __post_init__(self) -> None:
        if not self.code:
            raise PricingError("券码不能为空")
        if self.threshold < 0:
            raise PricingError("券门槛不能为负")
        if self.kind is CouponKind.THRESHOLD:
            if self.value <= 0:
                raise PricingError("满减金额必须为正")
            if self.value > self.threshold:
                raise PricingError("满减金额不应超过门槛")
        elif self.kind is CouponKind.PERCENT:
            if not (Decimal("0") < self.value < Decimal("1")):
                raise PricingError("折扣率必须在 (0, 1) 区间")
            if self.cap is not None and self.cap <= 0:
                raise PricingError("优惠上限必须为正")


@dataclass(frozen=True)
class Quote:
    subtotal: Decimal
    member_discount: Decimal
    coupon_discount: Decimal
    payable: Decimal


def subtotal(items: Iterable[LineItem]) -> Decimal:
    items = list(items)
    if not items:
        raise PricingError("订单不能为空")
    return to_cent(sum((item.amount for item in items), Decimal("0")))


def member_discount(amount: Decimal, level: MemberLevel) -> Decimal:
    if level not in MEMBER_RATE:
        raise PricingError(f"未知会员等级: {level}")
    if amount <= 0:
        return Decimal("0.00")
    return to_cent(amount * MEMBER_RATE[level])


def coupon_discount(amount_after_member: Decimal, coupon: Optional[Coupon]) -> Decimal:
    """返回券能抵扣的金额（不含门的判断失败时返回 0）。"""
    if coupon is None or amount_after_member <= 0:
        return Decimal("0.00")
    if amount_after_member < coupon.threshold:
        return Decimal("0.00")

    if coupon.kind is CouponKind.THRESHOLD:
        return to_cent(min(coupon.value, amount_after_member))

    raw = to_cent(amount_after_member * (Decimal("1") - coupon.value))
    if coupon.cap is not None:
        raw = min(raw, to_cent(coupon.cap))
    return to_cent(min(raw, amount_after_member))


def quote(
    items: Iterable[LineItem],
    level: MemberLevel = MemberLevel.NORMAL,
    coupon: Optional[Coupon] = None,
) -> Quote:
    base = subtotal(items)
    m_discount = member_discount(base, level)
    after_member = to_cent(base - m_discount)
    c_discount = coupon_discount(after_member, coupon)
    payable = to_cent(after_member - c_discount)

    if payable < 0:
        payable = Decimal("0.00")
    if base > 0 and payable < MIN_PAYABLE:
        payable = MIN_PAYABLE

    return Quote(
        subtotal=base,
        member_discount=m_discount,
        coupon_discount=c_discount,
        payable=payable,
    )


def parse_decimal(raw: str) -> Decimal:
    """外部输入（HTTP/表单）进入领域层的统一入口。"""
    try:
        return Decimal(raw)
    except (InvalidOperation, TypeError) as exc:
        raise PricingError(f"金额格式非法: {raw!r}") from exc
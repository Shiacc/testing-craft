"""L1 单元测试：优惠计算引擎。

本文件演示 6 个工程要点（对应 docs/07 §3-4）：
1. AAA 结构：Arrange / Act / Assert 三段清晰分离；
2. 参数化代替复制粘贴（@pytest.mark.parametrize）；
3. fixture 提供领域对象，避免每个用例重复构造；
4. 断言聚焦业务结果，不断言实现细节（不 mock 内部函数）；
5. marker 分组（smoke / edge / regression），供 CI 分层执行与分片；
6. 每个历史缺陷对应一条回归用例，用例名里写清缺陷编号。

运行：
    cd labs/01-unit-pytest
    python -m pytest -q                     # 全量
    python -m pytest -q -m "smoke"          # 只跑冒烟
    python -m pytest -q -m "not slow"       # 排除慢用例
    python -m pytest -q --cov=src --cov-report=term-missing
"""

from decimal import Decimal

import pytest

from src.pricing import (
    Coupon,
    CouponKind,
    LineItem,
    MemberLevel,
    PricingError,
    coupon_discount,
    member_discount,
    quote,
    subtotal,
    to_cent,
)


# ---------------------------------------------------------------- fixtures


@pytest.fixture()
def single_item() -> list[LineItem]:
    """一件 100.00 元的商品，便于口算校验。"""
    return [LineItem(sku="SKU-1", unit_price=Decimal("100.00"), quantity=1)]


@pytest.fixture()
def multi_item() -> list[LineItem]:
    """多行商品：99.99 × 2 + 0.01 × 3，小计 199.98 + 0.03 = 200.01。"""
    return [
        LineItem(sku="SKU-A", unit_price=Decimal("99.99"), quantity=2),
        LineItem(sku="SKU-B", unit_price=Decimal("0.01"), quantity=3),
    ]


@pytest.fixture()
def threshold_coupon() -> Coupon:
    """满 200 减 30。"""
    return Coupon(
        code="FULL200-30",
        kind=CouponKind.THRESHOLD,
        threshold=Decimal("200"),
        value=Decimal("30"),
    )


# ---------------------------------------------------------------- L1：小计


@pytest.mark.smoke
def test_subtotal_single_line(single_item):
    # Arrange 已在 fixture 中完成
    # Act
    amount = subtotal(single_item)
    # Assert
    assert amount == Decimal("100.00")


@pytest.mark.smoke
def test_subtotal_multiple_lines(multi_item):
    assert subtotal(multi_item) == Decimal("200.01")


@pytest.mark.edge
def test_subtotal_rounds_half_up_per_line():
    """每行先按分四舍五入再累加：0.005 的两行各记 0.01，合计 0.02（而非 0.005×2=0.01）。

    这条用例把"取整发生在哪一层"钉死。若有人把取整挪到汇总后再做，
    结果会变成 0.01，用例立刻失败 —— 这正是我们要的"设计约束可见化"。
    """
    items = [
        LineItem(sku="S1", unit_price=Decimal("0.005"), quantity=1),
        LineItem(sku="S2", unit_price=Decimal("0.005"), quantity=1),
    ]
    assert subtotal(items) == Decimal("0.02")


def test_subtotal_rejects_empty_cart():
    with pytest.raises(PricingError, match="订单不能为空"):
        subtotal([])


# ------------------------------------------------------------ L2：会员折扣


@pytest.mark.smoke
@pytest.mark.parametrize(
    ("level", "unit_price", "expected_discount", "expected_payable"),
    [
        (MemberLevel.NORMAL, "100.00", "0.00", "100.00"),
        (MemberLevel.SILVER, "100.00", "5.00", "95.00"),
        (MemberLevel.GOLD, "100.00", "10.00", "90.00"),
        (MemberLevel.BLACK, "100.00", "15.00", "85.00"),
        # 边界：折扣金额需要四舍五入 —— 33.33 × 15% = 4.9995 → 5.00
        (MemberLevel.BLACK, "33.33", "5.00", "28.33"),
    ],
)
def test_member_discount_matrix(level, unit_price, expected_discount, expected_payable):
    items = [LineItem(sku="SKU-1", unit_price=Decimal(unit_price), quantity=1)]

    result = quote(items, level=level)

    assert result.member_discount == Decimal(expected_discount)
    assert result.payable == Decimal(expected_payable)


def test_member_discount_of_zero_amount_is_zero():
    assert member_discount(Decimal("0"), MemberLevel.BLACK) == Decimal("0.00")


def test_unknown_member_level_rejected():
    with pytest.raises(PricingError, match="未知会员等级"):
        member_discount(Decimal("100"), "PLATINUM")  # type: ignore[arg-type]


# -------------------------------------------------------------- L3：优惠券


@pytest.mark.edge
def test_threshold_coupon_not_applied_below_threshold(single_item, threshold_coupon):
    """小计 100 < 门槛 200，券不生效，但不应报错。"""
    result = quote(single_item, MemberLevel.NORMAL, threshold_coupon)

    assert result.coupon_discount == Decimal("0.00")
    assert result.payable == Decimal("100.00")


@pytest.mark.smoke
def test_threshold_coupon_applied(multi_item, threshold_coupon):
    result = quote(multi_item, MemberLevel.NORMAL, threshold_coupon)

    assert result.subtotal == Decimal("200.01")
    assert result.coupon_discount == Decimal("30.00")
    assert result.payable == Decimal("170.01")


@pytest.mark.edge
def test_coupon_threshold_is_evaluated_after_member_discount(single_item):
    """关键业务规则：会员折扣先于券，门槛按"折后金额"判断。

    100 元商品 + 金卡（10%）→ 折后 90；满 100 减 20 的券因此不可用。
    这条用例锁定了"叠加顺序"，防止后续重构悄悄换成先券后折扣。
    """
    coupon = Coupon(
        code="FULL100-20",
        kind=CouponKind.THRESHOLD,
        threshold=Decimal("100"),
        value=Decimal("20"),
    )

    result = quote(single_item, MemberLevel.GOLD, coupon)

    assert result.member_discount == Decimal("10.00")
    assert result.coupon_discount == Decimal("0.00")
    assert result.payable == Decimal("90.00")


@pytest.mark.edge
def test_percent_coupon_respects_cap(single_item):
    """8 折券，优惠上限 5 元：100 × 20% = 20，被截断到 5。"""
    coupon = Coupon(
        code="P20-CAP5",
        kind=CouponKind.PERCENT,
        threshold=Decimal("0"),
        value=Decimal("0.8"),
        cap=Decimal("5"),
    )

    result = quote(single_item, MemberLevel.NORMAL, coupon)

    assert result.coupon_discount == Decimal("5.00")
    assert result.payable == Decimal("95.00")


@pytest.mark.edge
def test_percent_coupon_without_cap(single_item):
    coupon = Coupon(
        code="P10",
        kind=CouponKind.PERCENT,
        threshold=Decimal("0"),
        value=Decimal("0.9"),
    )

    result = quote(single_item, MemberLevel.NORMAL, coupon)

    assert result.coupon_discount == Decimal("10.00")
    assert result.payable == Decimal("90.00")


def test_coupon_discount_never_exceeds_amount():
    coupon = Coupon(
        code="FULL10-10",
        kind=CouponKind.THRESHOLD,
        threshold=Decimal("10"),
        value=Decimal("10"),
    )
    assert coupon_discount(Decimal("10.00"), coupon) == Decimal("10.00")
    assert coupon_discount(Decimal("9.99"), coupon) == Decimal("0.00")


# -------------------------------------------------------- L4：组合与兜底


@pytest.mark.smoke
def test_full_stack_member_plus_coupon(multi_item, threshold_coupon):
    """200.01 → 黑金 15%（30.00）→ 170.01 → 满 200 减 30 不满足 → 仍为 170.01。"""
    result = quote(multi_item, MemberLevel.BLACK, threshold_coupon)

    assert result.member_discount == Decimal("30.00")
    assert result.coupon_discount == Decimal("0.00")
    assert result.payable == Decimal("170.01")


@pytest.mark.edge
def test_payable_floor_is_one_cent():
    """券把金额打到 0 以下时，最低收 1 分（财务口径要求）。"""
    items = [LineItem(sku="CHEAP", unit_price=Decimal("1.00"), quantity=1)]
    coupon = Coupon(
        code="FULL1-1",
        kind=CouponKind.THRESHOLD,
        threshold=Decimal("1"),
        value=Decimal("1"),
    )

    result = quote(items, MemberLevel.NORMAL, coupon)

    assert result.coupon_discount == Decimal("1.00")
    assert result.payable == Decimal("0.01")


# ------------------------------------------------------------ L5：非法输入


@pytest.mark.edge
@pytest.mark.parametrize(
    ("quantity", "unit_price", "message"),
    [
        (0, "10.00", "数量必须为正整数"),
        (-1, "10.00", "数量必须为正整数"),
        (1, "-0.01", "单价不能为负"),
    ],
)
def test_invalid_line_item_rejected(quantity, unit_price, message):
    with pytest.raises(PricingError, match=message):
        LineItem(sku="SKU-X", unit_price=Decimal(unit_price), quantity=quantity)


@pytest.mark.edge
@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        (dict(kind=CouponKind.THRESHOLD, threshold=Decimal("100"), value=Decimal("0")), "满减金额必须为正"),
        (dict(kind=CouponKind.THRESHOLD, threshold=Decimal("10"), value=Decimal("20")), "满减金额不应超过门槛"),
        (dict(kind=CouponKind.PERCENT, threshold=Decimal("0"), value=Decimal("1.2")), "折扣率必须在"),
        (dict(kind=CouponKind.PERCENT, threshold=Decimal("0"), value=Decimal("0"), cap=None), "折扣率必须在"),
        (
            dict(kind=CouponKind.PERCENT, threshold=Decimal("0"), value=Decimal("0.9"), cap=Decimal("0")),
            "优惠上限必须为正",
        ),
    ],
)
def test_invalid_coupon_rejected(kwargs, message):
    with pytest.raises(PricingError, match=message):
        Coupon(code="BAD", **kwargs)


@pytest.mark.regression
def test_bug_1042_cent_drift_on_large_quantity():
    """缺陷 #1042 回归：数量放大后浮点累计误差导致多收 1 分。

    修复方式是全程使用 Decimal 并按分取整。此用例锁定修复，防止有人"为了性能"
    改回 float。
    """
    items = [LineItem(sku="BULK", unit_price=Decimal("0.07"), quantity=1000)]
    assert subtotal(items) == Decimal("70.00")


@pytest.mark.slow
def test_quote_is_deterministic_across_repeated_calls():
    """确定性检查：同一输入连续计算 200 次结果必须完全一致（无隐藏状态）。"""
    items = [LineItem(sku="SKU-1", unit_price=Decimal("19.99"), quantity=3)]
    baseline = quote(items, MemberLevel.GOLD)

    for _ in range(200):
        assert quote(items, MemberLevel.GOLD) == baseline


def test_to_cent_uses_half_up_rounding():
    assert to_cent(Decimal("1.005")) == Decimal("1.01")
    assert to_cent(Decimal("1.004")) == Decimal("1.00")

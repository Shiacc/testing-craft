"""L4 端到端测试：只覆盖"核心旅程 + 关键业务规则"。

数量刻意控制在 8 条以内 —— 这是 docs/02 与 docs/07 反复强调的取舍：
E2E 单条成本是单元测试的 10-20 倍，只用来验证"用户能不能走通"和"前后端规则是否一致"，
不用来穷举金额计算（那是 L1 的活）。

注意：全程没有 time.sleep()。Playwright 的 expect 会自动轮询，
手动 sleep 既慢又制造 flaky（docs/07 §3 的"确定性"要求）。
"""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import expect

from pages.checkout_page import CheckoutPage


@pytest.fixture()
def checkout(page, static_server) -> CheckoutPage:
    return CheckoutPage(page, static_server).open()


@pytest.mark.smoke
def test_page_renders_with_default_amounts(checkout):
    checkout.expect_amounts(subtotal="¥100.00", discount="-¥0.00", payable="¥100.00")
    expect(checkout.submit).to_be_enabled()


@pytest.mark.smoke
def test_quantity_changes_subtotal(checkout):
    checkout.set_quantity(3)

    checkout.expect_amounts(subtotal="¥300.00", discount="-¥0.00", payable="¥300.00")


@pytest.mark.smoke
def test_member_discount_applied(checkout):
    """金卡 10%：300 → 折 30 → 270。"""
    checkout.set_quantity(3).select_level("GOLD")

    checkout.expect_amounts(subtotal="¥300.00", discount="-¥30.00", payable="¥270.00")


@pytest.mark.edge
def test_coupon_applied_when_threshold_met(checkout):
    """满 300 减 50：普通会员 300 → 250。"""
    checkout.set_quantity(3).select_coupon("FULL300-50")

    checkout.expect_amounts(subtotal="¥300.00", discount="-¥50.00", payable="¥250.00")
    checkout.expect_coupon_hint("优惠券已生效")


@pytest.mark.edge
def test_coupon_blocked_by_member_discount(checkout):
    """前后端规则一致性检查：门槛按折后金额判断。

    300 元 + 黑金 15% → 255 < 300，满 300 减 50 应不可用。
    这条是 E2E 里最有价值的一类用例：它同时验证了"前端规则"与"后端规则"没有分叉。
    """
    checkout.set_quantity(3).select_level("BLACK").select_coupon("FULL300-50")

    checkout.expect_amounts(subtotal="¥300.00", discount="-¥45.00", payable="¥255.00")
    checkout.expect_coupon_hint("当前折后金额未达门槛，优惠券不可用")


@pytest.mark.edge
def test_invalid_quantity_shows_error_and_blocks_submit(checkout):
    checkout.set_quantity(0)

    expect(checkout.message).to_have_text("数量必须为正整数")
    expect(checkout.submit).to_be_disabled()


@pytest.mark.smoke
def test_submit_order_shows_order_id(checkout):
    """验证异步提交：点击后文案先变"提交中…"，随后出现订单号（自动等待，不写 sleep）。"""
    checkout.set_quantity(2).select_level("SILVER")

    checkout.submit_order()

    expect(checkout.message).to_have_text("下单成功")
    expect(checkout.order_id).to_have_text(re.compile(r"^ord_[a-z0-9]{8}$"))


def test_full_journey_gold_card_with_coupon(checkout):
    """核心旅程：选品 → 选会员 → 选券 → 提交，一条用例串起完整链路。"""
    checkout.set_quantity(4).select_level("GOLD").select_coupon("FULL300-50")

    # 400 → 金卡 40 → 360 → 满 300 减 50 → 310
    checkout.expect_amounts(subtotal="¥400.00", discount="-¥90.00", payable="¥310.00")

    checkout.submit_order()

    expect(checkout.message).to_have_text("下单成功")
    expect(checkout.order_id).not_to_be_empty()
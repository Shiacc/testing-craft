"""Page Object：把"页面怎么操作"与"验证什么"分开。

POM 的价值不在封装本身，而在两点：
1. 页面结构变化时只改一处（改这里，而不是改 20 个测试）；
2. 测试读起来是业务语言（`checkout.select_level("GOLD")`），而不是 CSS 选择器噪声。

选择器统一走 data-testid：不依赖样式类名、不依赖布局、不依赖文案。
"""

from __future__ import annotations

from playwright.sync_api import Page, expect


class CheckoutPage:
    PATH = "/index.html"

    def __init__(self, page: Page, base_url: str) -> None:
        self.page = page
        self.base_url = base_url
        self.qty = page.get_by_test_id("qty")
        self.level = page.get_by_test_id("level")
        self.coupon = page.get_by_test_id("coupon")
        self.coupon_hint = page.get_by_test_id("coupon-hint")
        self.subtotal = page.get_by_test_id("subtotal")
        self.discount = page.get_by_test_id("discount")
        self.payable = page.get_by_test_id("payable")
        self.submit = page.get_by_test_id("submit")
        self.message = page.get_by_test_id("message")
        self.order_id = page.get_by_test_id("order-id")

    # ---------------------------------------------------------- 导航

    def open(self) -> "CheckoutPage":
        self.page.goto(self.base_url + self.PATH)
        expect(self.page).to_have_title("结算台 · Labs 03")
        return self

    # ---------------------------------------------------------- 操作

    def set_quantity(self, qty: int) -> "CheckoutPage":
        self.qty.fill(str(qty))
        self.qty.blur()
        return self

    def select_level(self, level: str) -> "CheckoutPage":
        self.level.select_option(level)
        return self

    def select_coupon(self, code: str) -> "CheckoutPage":
        self.coupon.select_option(code)
        return self

    def submit_order(self) -> "CheckoutPage":
        self.submit.click()
        return self

    # ---------------------------------------------------------- 断言辅助

    def expect_amounts(self, *, subtotal: str, discount: str, payable: str) -> None:
        expect(self.subtotal).to_have_text(subtotal)
        expect(self.discount).to_have_text(discount)
        expect(self.payable).to_have_text(payable)

    def expect_coupon_hint(self, text: str) -> None:
        expect(self.coupon_hint).to_have_text(text)
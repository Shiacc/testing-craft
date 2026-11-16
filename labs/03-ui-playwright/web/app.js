/**
 * 结算台前端逻辑（Labs 03 被测行为）。
 *
 * 工程要点：
 * 1. 金额用「分」为单位的整数计算，避免 0.1+0.2 这类浮点误差；
 * 2. 所有可被测试定位的元素都带 data-testid —— 测试不依赖 CSS 类名与文案；
 * 3. 提交时用 setTimeout 模拟 150ms 网络往返，用于演示 Playwright 的自动等待
 *    （测试里不写 sleep，靠 expect 轮询）。
 */

(function () {
  const UNIT_PRICE_CENT = 10000; // 100.00 元

  const MEMBER_RATE = { NORMAL: 0, SILVER: 0.05, GOLD: 0.1, BLACK: 0.15 };

  const COUPONS = {
    "FULL100-20": { thresholdCent: 10000, valueCent: 2000 },
    "FULL300-50": { thresholdCent: 30000, valueCent: 5000 },
  };

  // 与测试共用同一套 data-testid 契约：前端改结构不必改测试，改语义必须两边一起改
  const $ = (testId) => document.querySelector('[data-testid="' + testId + '"]');
  const yuan = (cent) => "¥" + (cent / 100).toFixed(2);

  const els = {
    unitPrice: $("unit-price"),
    qty: $("qty"),
    level: $("level"),
    coupon: $("coupon"),
    couponHint: $("coupon-hint"),
    calc: $("calc"),
    subtotal: $("subtotal"),
    discount: $("discount"),
    payable: $("payable"),
    submit: $("submit"),
    message: $("message"),
    orderId: $("order-id"),
  };

  function compute() {
    const qty = Number(els.qty.value);
    if (!Number.isInteger(qty) || qty <= 0) {
      return { valid: false, error: "数量必须为正整数" };
    }

    const subtotalCent = UNIT_PRICE_CENT * qty;
    const memberCent = Math.round(subtotalCent * MEMBER_RATE[els.level.value]);
    const afterMember = subtotalCent - memberCent;

    const coupon = COUPONS[els.coupon.value];
    let couponCent = 0;
    let couponApplicable = true;
    if (coupon) {
      couponApplicable = afterMember >= coupon.thresholdCent;
      if (couponApplicable) {
        couponCent = Math.min(coupon.valueCent, afterMember);
      }
    }

    let payableCent = afterMember - couponCent;
    if (payableCent < 1) payableCent = 1;

    return {
      valid: true,
      subtotalCent,
      discountCent: subtotalCent - payableCent,
      payableCent,
      couponApplicable,
      hasCoupon: Boolean(coupon),
    };
  }

  function render() {
    const result = compute();

    if (!result.valid) {
      els.message.textContent = result.error;
      els.message.className = "message error";
      els.submit.disabled = true;
      els.couponHint.textContent = "";
      els.subtotal.textContent = yuan(UNIT_PRICE_CENT * Math.max(Number(els.qty.value) || 0, 0));
      els.discount.textContent = "-¥0.00";
      els.payable.textContent = "-";
      return;
    }

    els.submit.disabled = false;
    els.message.textContent = "";
    els.message.className = "message";
    els.orderId.textContent = "";

    els.subtotal.textContent = yuan(result.subtotalCent);
    els.discount.textContent = "-" + yuan(result.discountCent);
    els.payable.textContent = yuan(result.payableCent);

    if (result.hasCoupon && !result.couponApplicable) {
      els.couponHint.textContent = "当前折后金额未达门槛，优惠券不可用";
      els.couponHint.className = "hint warn";
    } else if (result.hasCoupon) {
      els.couponHint.textContent = "优惠券已生效";
      els.couponHint.className = "hint";
    } else {
      els.couponHint.textContent = "";
      els.couponHint.className = "hint";
    }
  }

  function submit() {
    const result = compute();
    if (!result.valid) return;

    els.submit.disabled = true;
    els.message.textContent = "提交中…";
    els.message.className = "message";

    // 模拟网络往返：让 Playwright 的自动等待真正被验证到
    setTimeout(function () {
      const suffix = Math.random().toString(36).slice(2, 10);
      els.message.textContent = "下单成功";
      els.message.className = "message ok";
      els.orderId.textContent = "ord_" + suffix;
      els.submit.disabled = false;
    }, 150);
  }

  ["qty", "level", "coupon"].forEach(function (key) {
    els[key].addEventListener("input", render);
    els[key].addEventListener("change", render);
  });
  els.calc.addEventListener("click", render);
  els.submit.addEventListener("click", submit);

  render();
})();
# Lab 03 · L4 端到端测试（Playwright）

> **一句话**：8 条 E2E 覆盖核心旅程与关键业务规则，全程没有一个 `sleep`，失败自动留截图与 trace。
>
> 对应文档：[03 §4 UI 框架](../../docs/03-test-frameworks.md)、[07 §9 flaky 治理](../../docs/07-engineering-and-ci.md)

---

## 1. 被测页面

`web/index.html` + `web/app.js` —— 结算台（静态页，无需构建、无需 Node）：

- 输入数量、选择会员等级与优惠券 → 实时显示小计 / 优惠 / 应付；
- 金额计算用**整数分**实现，与后端规则保持一致（门槛按折后金额判断）；
- 提交后模拟 150ms 网络往返，返回 `ord_xxxxxxxx` 订单号；
- 非法数量（≤ 0）→ 错误提示并禁用提交。

所有可测元素都带 `data-testid`，**前端与测试共用同一套定位契约**。

---

## 2. 运行

```bash
cd labs/03-ui-playwright

python -m pytest -q                    # 8 passed，约 3-4s
python -m pytest -q -m smoke           # 只跑核心旅程
HEADED=1 python -m pytest -q           # 打开真实窗口观察（PowerShell: $env:HEADED="1"）
PW_CHANNEL=msedge python -m pytest -q  # 指定浏览器通道
```

浏览器选择顺序：`PW_CHANNEL` 指定 → Playwright 自带 chromium → 系统 Chrome → 系统 Edge。
**自动回落不是偷懒**：CI 沙箱常常下载不到浏览器二进制，硬编码 `chromium.launch()` 会让整条流水线直接红。

失败时产物在 `artifacts/`：`<用例名>.png` 与 `<用例名>-trace.zip`。
查看 trace：`python -m playwright show-trace artifacts\xxx-trace.zip`。

---

## 3. 用例清单（刻意只有 8 条）

| 用例 | 覆盖的旅程 / 规则 |
|---|---|
| `test_page_renders_with_default_amounts` | 首屏默认态 |
| `test_quantity_changes_subtotal` | 小计随数量变化 |
| `test_member_discount_applied` | 会员折扣 |
| `test_coupon_applied_when_threshold_met` | 优惠券生效 |
| `test_coupon_blocked_by_member_discount` | **前后端规则一致性**（门槛按折后判断） |
| `test_invalid_quantity_shows_error_and_blocks_submit` | 客户端校验与按钮禁用 |
| `test_submit_order_shows_order_id` | 异步提交 + 自动等待 |
| `test_full_journey_gold_card_with_coupon` | 完整旅程串联 |

> 为什么不做金额穷举？那是 Lab 01 的活。E2E 单条成本是单元测试的 10-20 倍，
> 只用来验证"用户能不能走通"和"两层规则是否分叉"（[02 §6](../../docs/02-when-to-automate.md)）。

---

## 4. 目录

```
03-ui-playwright/
├── README.md
├── pytest.ini
├── web/
│   ├── index.html        # 结算台
│   └── app.js            # 价格计算与提交（整数分）
└── tests/
    ├── conftest.py       # 静态服务器 + 浏览器回落链 + 失败留证钩子
    ├── pages/
    │   └── checkout_page.py   # Page Object
    └── test_checkout_flow.py  # 8 条用例
```

---

## 5. 三个关键设计

1. **`data-testid` 而不是 CSS/文案**：改样式、改布局、改文案都不影响测试；改语义才需要两边一起改。
   前端 `app.js` 也用 `[data-testid]` 取元素，让这条契约真正"共用"。
2. **不写 `sleep`**：Playwright 的 `expect(...).to_have_text()` 自动轮询。
   手动 sleep 既拖慢又快不过真实网络——这是 E2E flaky 的头号成因。
3. **失败才留证**：`pytest_runtest_makereport` 判断失败后，才在 fixture teardown 里截图 + 存 trace。
   成功用例不留垃圾，失败用例证据齐全——这才叫"可观测"。

---

## 6. 常见坑

| 现象 | 原因 | 处理 |
|---|---|---|
| 本地过、CI 挂 | 浏览器二进制缺失 | 回落链 + `PW_CHANNEL`，CI 里预装浏览器 |
| 偶发失败 | 固定端口冲突 | 服务器绑 `port=0` 随机端口 |
| 偶发失败 | 服务器未就绪就发请求 | `static_server` 里有 TCP 就绪探测 |
| 用例互相污染 | 共享 cookie/localStorage | 每个用例新 `context`（本实验如此） |
| 断言拿到旧值 | 用 `inner_text()` 后立即断言 | 改用 `expect(locator).to_have_text()` |

---

## 7. 延伸练习

- 把 `test_coupon_blocked_by_member_discount` 对应规则在前端改成"按原价判断门槛"，观察这条用例如何发现前后端分叉。
- 给页面加一个"提交失败"分支（如随机 10% 失败），写一条用例断言重试按钮出现。
- 引入视觉回归（`expect(page).to_have_screenshot()`），并讨论：什么页面值得做视觉断言，什么页面不值得。
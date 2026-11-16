# Lab 01 · L1 单元测试（pytest）

> **一句话**：用 30 条毫秒级用例锁住"优惠金额怎么算"，证明"能在单元层证明的问题不该上 UI"。
>
> 对应文档：[02 §6 分层优先级](../../docs/02-when-to-automate.md)、[07 §3 测试代码工程标准](../../docs/07-engineering-and-ci.md)

---

## 1. 被测对象

`src/pricing.py` —— 订单优惠计算引擎（纯函数、无 IO）：

| 规则 | 说明 |
|---|---|
| 小计 | Σ(单价 × 数量)，**每行先按分四舍五入再累加** |
| 会员折扣 | 普通 0% / 银 5% / 金 10% / 黑金 15% |
| 优惠券 | 满减券（门槛 + 减免）、折扣券（折扣率 + 上限） |
| 叠加顺序 | **先会员折扣，再优惠券**，券门槛按"折后金额"判断 |
| 兜底 | 应付不为负；正价订单最低 1 分 |
| 非法输入 | 数量 ≤ 0、单价 < 0、未知等级、券参数非法 → `PricingError` |

金额全程 `Decimal`，取整统一走 `to_cent()`（`ROUND_HALF_UP`）。

---

## 2. 这个实验演示什么

| 要点 | 在哪个用例里 |
|---|---|
| 参数化代替复制粘贴 | `test_member_discount_matrix`、`test_invalid_line_item_rejected` |
| fixture 提供领域对象 | `single_item` / `multi_item` / `threshold_coupon` |
| marker 分层（CI 可选择性执行） | `@pytest.mark.smoke / edge / regression / slow` |
| **把设计约束钉死** | `test_subtotal_rounds_half_up_per_line`（取整发生在行级还是汇总级） |
| **锁定业务顺序** | `test_coupon_threshold_is_evaluated_after_member_discount` |
| 缺陷 → 永久回归用例 | `test_bug_1042_cent_drift_on_large_quantity` |
| 确定性检查 | `test_quote_is_deterministic_across_repeated_calls` |
| 断言业务结果而非实现细节 | 全部用例都不 mock `pricing` 内部函数 |

---

## 3. 运行

```bash
cd labs/01-unit-pytest

python -m pytest -q                        # 全量：30 passed
python -m pytest -q -m smoke               # 冒烟：< 30s
python -m pytest -q -m "not slow"          # 排除慢用例
python -m pytest -q --cov=src --cov-report=term-missing
python -m pytest -q --junitxml=report.xml  # CI 口径
```

Windows 下若中文输出乱码：`python -X utf8 -m pytest -q` 或 `$env:PYTHONUTF8="1"`。

---

## 4. 目录

```
01-unit-pytest/
├── README.md
├── pytest.ini          # pythonpath、marker 声明、严格模式
├── src/
│   └── pricing.py      # 被测对象（纯函数）
└── tests/
    └── test_pricing.py # 30 条用例
```

---

## 5. 三个值得记住的细节

1. **`--strict-markers` + 显式声明 marker**：写错 marker 名会直接报错，而不是被静默忽略——
   CI 里"筛选条件写错导致一条都没跑"是真实事故。
2. **断言差异要能读**：`assert result.payable == Decimal("170.01")` 失败时会直接显示期望与实际，
   比 `assert result.payable == 17001`（分）更省心。内部用分、对外用元，是常见做法，别让测试去翻译。
3. **缺陷回归用例要写缺陷编号和原因**：`test_bug_1042_...` 的注释说明了"为什么不能用 float"。
   一年后有人想"优化性能"时，这条注释是唯一的刹车。

---

## 6. 常见坑

| 现象 | 原因 | 处理 |
|---|---|---|
| `ModuleNotFoundError: src` | 没在实验目录下运行，或 `pytest.ini` 的 `pythonpath` 失效 | 在 `labs/01-unit-pytest` 下运行 |
| 单跑通过、全跑失败 | 用例间共享可变状态 | 本实验全部用冻结 dataclass，无共享状态 |
| 断言到处是 `0.1 + 0.2` 误差 | 用了 float | 统一 `Decimal` + `to_cent()` |
| 覆盖率 100% 但抓不到 bug | 断言过弱 | 引入变异测试验证"断言杀伤力"（[03 §7](../../docs/03-test-frameworks.md)） |

---

## 7. 延伸练习

- 加一条"折扣券上限高于订单金额"的用例，确认兜底为 1 分。
- 把 `member_discount` 里的 `ROUND_HALF_UP` 改成 `ROUND_DOWN`，看哪几条用例变红（这就是回归网的作用）。
- 用 `mutmut` 或 `pytest-mutmut` 跑一遍，观察"覆盖率 100% 但变异存活"的用例有多少。
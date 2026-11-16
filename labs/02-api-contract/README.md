# Lab 02 · L3 接口与契约测试（FastAPI + pytest）

> **一句话**：35 条用例、0.16 秒，覆盖鉴权、非法输入、幂等重放、资源归属、状态机、契约形状——
> 证明"接口层是企业性价比最高的主干"。
>
> 对应文档：[03 §3 接口测试框架](../../docs/03-test-frameworks.md)、[07 §4 测试数据治理](../../docs/07-engineering-and-ci.md)

---

## 1. 被测服务

`app/main.py` —— 最小订单服务（FastAPI，内存存储）：

| 接口 | 语义要点 |
|---|---|
| `GET /health` | 存活探测 |
| `POST /api/quote` | 试算，不落单；金卡 10% 折扣，券门槛按折后金额判断 |
| `POST /api/orders` | 下单，**必须带 `Idempotency-Key`**；同键同体 → 返回同一订单；同键异体 → 409 |
| `GET /api/orders/{id}` | 查单；不存在 404，他人订单 403 |
| `POST /api/orders/{id}/pay` | 状态机 CREATED → PAID；重复支付 409 |

统一错误体：`{"code": "UPPER_SNAKE", "message": "..."}`，框架默认的 422 被收敛为 400。

测试令牌：`tok-alice`（金卡）、`tok-bob`（普通），用 `Authorization: Bearer <token>`。

---

## 2. 演示的六类用例矩阵

| 类别 | 示例用例 | 断言重点 |
|---|---|---|
| 正常路径 | `test_quote_happy_path` | 金额精确到分、字段齐全 |
| 边界 | `test_quote_coupon_threshold_evaluated_after_member_discount` | 门槛按折后金额判断 |
| 非法输入 | `test_quote_rejects_invalid_payload_with_400`（6 组参数化） | **400 而非 422**、`code` 稳定 |
| 鉴权 | `test_quote_requires_token` / `test_quote_rejects_invalid_token` | 401 且区分 `UNAUTHENTICATED` / `INVALID_TOKEN` |
| 幂等 | `test_create_order_is_idempotent_on_replay`、`..._reuse_with_different_body_conflicts` | 同键返回同一 `order_id`；异体 409 |
| 归属与状态机 | `test_get_other_users_order_forbidden`、`test_paid_to_paid_rejected` | 403 / 409，**不能静默成功** |

契约测试（`tests/test_contract.py`）单独一层：

- 接口清单必须与 `EXPECTED_PATHS` 一致（**删除接口会立刻失败**）；
- 响应字段集合必须与 OpenAPI 一致（**字段被删/被改名会被抓到**）；
- 错误码必须出现在 OpenAPI 的 `responses` 里（**迫使你文档化错误码**）；
- 错误体形状统一（`code` + `message`）。

---

## 3. 运行

```bash
cd labs/02-api-contract

python -m pytest -q                     # 35 passed，约 0.16s
python -m pytest -q -m smoke            # 冒烟子集
python -m pytest -q tests/test_contract.py -v
python -m pytest -q --junitxml=report.xml
```

想看真实 HTTP：`python -m uvicorn app.main:app --port 8000`，然后打开 `http://127.0.0.1:8000/docs`。

---

## 4. 目录

```
02-api-contract/
├── README.md
├── pytest.ini
├── app/
│   └── main.py              # 被测服务（含 OpenAPI 声明）
└── tests/
    ├── conftest.py          # 客户端、鉴权头、请求体工厂、**用例级状态重置**
    ├── test_orders_api.py   # 鉴权/试算/下单/查询
    ├── test_state_machine.py# 状态迁移矩阵
    └── test_contract.py     # 契约与形状
```

---

## 5. 三个关键设计

1. **`autouse` 状态重置**（`_reset_state`）：订单、幂等键、库存每个用例前清空。
   不这么做，第 3 条用例就会因为"库存被前两条扣光"而随机失败——这是接口测试最典型的 flaky 来源。
2. **错误码是契约的一部分**：`RequestValidationError` → 400 + `INVALID_PAYLOAD`。
   理由很实际：前端只需处理一套错误结构；而且 `422` 在网关层常被改写，不可依赖。
3. **幂等用指纹校验**：`sha256(排序后的请求体)`。同键异体返回 409 而不是"悄悄按新请求处理"，
   否则客户端重试时的语义会变得不可预测。

---

## 6. 常见坑

| 现象 | 原因 | 处理 |
|---|---|---|
| 用例随机失败 | 共享内存状态 / 固定账号 / 固定订单号 | 见 `_reset_state`，用例自造数据 |
| 本地过、CI 挂 | 依赖真实端口/外部服务 | 用 `TestClient` 直连 ASGI，不起端口 |
| 契约测试"永远绿" | 只校验了 200，没校验字段集合 | 断言 `set(body.keys()) == EXPECTED` |
| 幂等测试误判 | 只比状态码，没比 `order_id` | 断言"同一订单"而不只是"返回 2xx" |

---

## 7. 延伸练习

- 给 `POST /api/orders` 加"库存扣减失败要回滚"的用例（提示：造一个扣减中途抛错的 SKU）。
- 把 `EXPECTED_PATHS` 里删掉 `/health`，观察契约测试如何报错（这就是"防误删"的价值）。
- 用 `schemathesis` 对 OpenAPI 做属性测试，看能否自动找出未覆盖的非法输入组合。
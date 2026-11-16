# Labs · 6 个可运行实验

> 同一业务域（**订单优惠计算**）贯穿 L1→L5，再从"流水线"走到"平台"。
> 每个实验独立可跑、互不依赖，全部在 Windows + Python 3.12 实测通过。

| # | 实验 | 层级 / 角色 | 演示的核心能力 | 对应文档 | 单次耗时 |
|---|---|---|---|---|---|
| 01 | [`01-unit-pytest`](01-unit-pytest/README.md) | L1 单元 | 参数化、fixture、marker 分层、覆盖率、缺陷回归用例 | [02 §6](../docs/02-when-to-automate.md)、[07 §3-4](../docs/07-engineering-and-ci.md) | < 1s |
| 02 | [`02-api-contract`](02-api-contract/README.md) | L3 接口 / 契约 | 六类用例矩阵、统一错误体、幂等、状态机、OpenAPI 契约锁 | [03 §3](../docs/03-test-frameworks.md) | < 1s |
| 03 | [`03-ui-playwright`](03-ui-playwright/README.md) | L4 E2E | Page Object、`data-testid` 契约、自动等待、失败留证 | [03 §4](../docs/03-test-frameworks.md)、[07 §9](../docs/07-engineering-and-ci.md) | ~4s |
| 04 | [`04-perf`](04-perf/README.md) | L5 性能 | 阈值门禁（不是出报告）、随机端口起服务、k6 对照脚本 | [08 §5](../docs/08-metrics-and-quality-gates.md) | ~2s |
| 05 | [`05-ci`](05-ci/README.md) | 流程层 | 分层、分片、并行、JUnit 聚合、门禁；Gitee Go / GitHub Actions | [07 §6-8](../docs/07-engineering-and-ci.md) | ~7s |
| 06 | [`06-platform-lite`](06-platform-lite/README.md) | 平台层 | 资产登记、调度分片、执行池、结果存储、HTML 报告、历史趋势、门禁 | [04](../docs/04-test-platforms.md) | ~5s |

---

## 1. 环境准备

```bash
python -m pip install -r labs/requirements.txt
```

浏览器：03 实验优先用 Playwright 自带 chromium；**未下载时自动回落系统 Chrome / Edge**，
也可显式指定：`PW_CHANNEL=msedge python -m pytest -q`（Windows PowerShell：`$env:PW_CHANNEL="msedge"`）。

可选：`k6`（仅 04 实验的 `k6/load_test.js` 需要，未安装不影响 Python 版本）。

---

## 2. 三种跑法

**① 逐层跑（学习时用）**

```bash
cd labs/01-unit-pytest       && python -m pytest -q
cd labs/02-api-contract      && python -m pytest -q
cd labs/03-ui-playwright     && python -m pytest -q
cd labs/04-perf              && python py_load_smoke.py
```

**② 一条命令跑全部（体验流水线）**

```bash
cd labs/05-ci
python scripts/ci_local.py --shards 2 --with-perf
# 产物：labs/05-ci/.ci-results/summary.md
```

**③ 平台视角（体验"谁跑了什么、能不能发"）**

```bash
cd labs/06-platform-lite
python -m platform_lite list
python -m platform_lite run --tag smoke --parallel 4 --shards 2
python -m platform_lite history
# 产物：runs/<run_id>/report.html
```

---

## 3. 建议学习路径

| 你的角色 | 建议顺序 |
|---|---|
| 刚接触自动化 | 01 → 02 → 03，重点看"每个用例在验证什么风险" |
| 想搭 CI | 02 → 03 → 05，重点看分片与门禁的设计取舍 |
| 在做技术选型 | 01 → 04 → 06，对比"框架能力"与"平台能力"的边界 |
| 带团队 / 做治理 | 先读 [08](../docs/08-metrics-and-quality-gates.md)、[09](../docs/09-organization-and-governance.md)，再跑 05、06 建立直觉 |

---

## 4. 六个实验分别证明了什么

1. **01**：一条毫秒级单元测试就能锁住"金额怎么算"，不需要开浏览器。**证明"分层"不是理论**。
2. **02**：接口测试用 35 条用例覆盖鉴权/非法输入/幂等/归属/状态机，运行 0.16 秒。**证明"接口层性价比最高"**。
3. **03**：8 条 E2E 覆盖核心旅程，且能在前后端规则分叉时立刻发现。**证明"E2E 只做少量关键路径"**。
4. **04**：性能结果直接转换成退出码。**证明"阈值门禁"与"报告装饰"的区别**。
5. **05**：分片 2 后墙面耗时下降，且任一层红即阻断。**证明"流程层可以更换（Gitee Go / GitHub Actions），资产不应被绑定"**。
6. **06**：把 01-04 全部登记成资产，统一调度、统一报告、统一门禁、留趋势。**证明"平台是组织层，不是更高级的框架"**。

---

## 5. 实验仓库的三条取舍（有意为之）

1. **实验之间不互相 import**。领域逻辑在 01 与 02 各有最小实现——为了"每个实验都能单独 clone 出来跑"。
   真实项目应抽公共领域库，这件事在 [07 §2](../docs/07-engineering-and-ci.md) 讲。
2. **数据全部内存态**。用数据库会把"跑起来"的门槛抬到 Docker，与"5 分钟上手"冲突。
   数据隔离的做法与原则在 [07 §4](../docs/07-engineering-and-ci.md)。
3. **不做视觉回归与安全扫描的实跑**。它们在 03 与 04 的文档里说明何时值得引入，但实验不做，
   因为这两类最容易产生"看起来高级但没人维护"的资产（见 [10 §A5](../docs/10-anti-patterns.md)）。

---

## 6. 运行产物说明

| 目录 | 来源 | 是否入库 |
|---|---|---|
| `labs/05-ci/.ci-results/` | JUnit XML + summary.md | 否（`.gitignore`） |
| `labs/06-platform-lite/runs/` | results.json / report.html / history.jsonl | 否 |
| `labs/03-ui-playwright/artifacts/` | 失败截图与 trace.zip | 否 |

清空产物：`git clean -fdX labs/`（会删除所有被忽略文件，执行前确认）。
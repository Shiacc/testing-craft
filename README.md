# Testing Craft · 测试框架与测试平台实践

> 一份可以直接拿去用的企业级测试知识体系 + 6 个可运行实验。
> 回答三个问题：**要不要自动化、用什么工具、怎么在组织里跑起来。**

[![docs](https://img.shields.io/badge/docs-11%20篇-2563eb)](docs/index.md)
[![labs](https://img.shields.io/badge/labs-6%20个-0f9d58)](labs/README.md)
[![python](https://img.shields.io/badge/python-3.11%2B-3776ab)](labs/requirements.txt)

---

## 这个仓库是什么

一条完整的因果链，而不是一堆工具说明书：

```
概念（框架 vs 平台） → 决策（该不该自动化） → 工具（框架/平台全景）
        → 选型（加权评分 + TCO） → 落地（工程/CI/度量/治理） → 反模式
```

- **`docs/`**：11 篇文档，从术语定义讲到组织治理，表格密集、结论可检验；入口是 [docs/index.md](docs/index.md)。
- **`labs/`**：6 个实验，同一业务域（订单优惠计算）贯穿 L1→L5，从一条单元测试一路做到一个能出门禁的最小测试平台。

两者严格对应：文档只给结论与最小片段，完整实现都在 labs，避免文档与代码双维护漂移。

---

## 5 分钟上手

```bash
git clone git@gitee.com:zhuge-x/testing-craft.git
cd testing-craft
python -m pip install -r labs/requirements.txt

# 1) 单层：跑单元测试
cd labs/01-unit-pytest && python -m pytest -q

# 2) 端到端：UI 核心旅程（自动回落系统 Chrome/Edge）
cd labs/03-ui-playwright && python -m pytest -q

# 3) 一条命令跑完所有层，带分片、并行、门禁
cd labs/05-ci && python scripts/ci_local.py --shards 2 --with-perf

# 4) 平台视角：登记表 → 调度 → 执行 → 报告 → 门禁 → 趋势
cd labs/06-platform-lite
python -m platform_lite list
python -m platform_lite run --tag smoke --parallel 4 --shards 2
python -m platform_lite history
```

---

## 目录

```
testing-craft/
├── docs/                     11 篇企业级文档
│   ├── index.md              文档索引页（脚本生成：按问题/角色/小节查）
│   ├── README.md             导读：核心结论速查、约定、变更记录
│   ├── 01-concepts-and-terminology.md
│   ├── 02-when-to-automate.md
│   ├── 03-test-frameworks.md
│   ├── 04-test-platforms.md
│   ├── 05-selection-methodology.md
│   ├── 06-scenario-playbook.md
│   ├── 07-engineering-and-ci.md
│   ├── 08-metrics-and-quality-gates.md
│   ├── 09-organization-and-governance.md
│   ├── 10-anti-patterns.md
│   └── 11-templates-and-checklists.md
└── labs/                     6 个可运行实验
    ├── 01-unit-pytest/       L1 单元测试：参数化、fixture、marker、覆盖率、缺陷回归
    ├── 02-api-contract/      L3 接口与契约：错误码矩阵、幂等、状态机、OpenAPI 契约
    ├── 03-ui-playwright/     L4 E2E：Page Object、data-testid、失败留证（截图 + trace）
    ├── 04-perf/              L5 性能：阈值门禁（Python 冒烟 + k6 脚本）
    ├── 05-ci/                流程层：分层/分片/并行/门禁 + Gitee Go / GitHub Actions
    └── 06-platform-lite/     平台层：资产管理、调度、执行池、报告、门禁、趋势
```

---

## 核心结论（完整论证见 [docs/README.md](docs/README.md#4-核心结论速查)）

1. 自动化的本质是把「判定」变成可复用资产——判定不确定或不会重复执行，就不该自动化。
2. **在能证明问题的最底层断言**：单元 > 接口 > UI。接口层通常是企业性价比最高的主干。
3. **测试框架是进程内的代码库，测试平台是独立部署的服务系统**，二者分层而非替代。
4. 平台解决「谁在什么环境跑了什么、结果如何、能不能放行」，**不解决用例质量**。
5. 先有框架 + CI + 度量，再评估平台；没有用例资产的平台只是空壳。
6. **flaky 用例比没有用例更伤**，它训练团队忽略红灯。
7. 门禁要少而硬（≤3 条），度量看结果（逃逸缺陷、变更失败率），不看用例数。

---

## 实验验证状态

本仓库的每个实验都在本地实际执行过（Windows + Python 3.12），下表是执行结果，不是"设计上应该能跑"：

| 实验 | 命令 | 实测结果（数字随机器负载浮动） |
|---|---|---|
| 01 单元 | `pytest -q` | 30 passed，0.07s |
| 02 接口/契约 | `pytest -q` | 35 passed，0.2-0.4s |
| 03 E2E | `pytest -q` | 8 passed，3-5s（msedge 通道） |
| 04 性能 | `py_load_smoke.py` | p95 约 17-20ms / 200-270 req/s / 错误率 0%，门禁 PASS |
| 05 CI 编排 | `ci_local.py --shards 2 --with-perf` | 73 用例全绿，墙面 6-12s，门禁 PASS |
| 06 平台 | `platform_lite run --tag smoke` | 73 用例，4-6s，门禁 PASS，生成 HTML 报告与趋势 |

未在本机执行的只有 `labs/04-perf/k6/load_test.js`（未安装 k6 二进制），脚本按 k6 v0.50+ 语法编写，需自行 `k6 run` 验证。

---

## 环境要求

- Python **3.11+**（开发验证于 3.12）
- 依赖：`pytest`、`pytest-cov`、`fastapi`、`uvicorn`、`httpx`、`playwright`、`pyyaml`
- 浏览器：Playwright 自带 chromium，未安装时自动回落到系统 Chrome / Edge（`PW_CHANNEL=msedge` 可强制）
- 可选：`k6`（仅 04 实验的 k6 脚本需要）

---

## 约定

- 文档用中文，代码与标识符用英文；金额一律用 `Decimal`（Python）/ 整数分（JS），不出现浮点累计。
- 每个实验都能独立运行，互不 import（复制少量领域逻辑换取"零耦合可跑"，这是实验仓库的取舍）。
- `labs/` 下的 `.ci-results/`、`runs/`、`artifacts/` 属于运行产物，已在 `.gitignore` 中排除。

---

## 许可

内部学习与工程参考用途。引用请注明来源。
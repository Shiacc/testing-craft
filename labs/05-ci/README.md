# Lab 05 · 流程层：CI 编排与门禁

> **一句话**：同一套"分层 + 分片 + 并行 + 门禁"的意图，可以用本地脚本、Gitee Go、GitHub Actions 三种方式表达——
> **流程可以换，用例资产不该被流程绑架**。
>
> 对应文档：[07 §6 CI 阶段设计](../../docs/07-engineering-and-ci.md)、[07 §7 并行与分片](../../docs/07-engineering-and-ci.md)

---

## 1. 三个文件的分工

| 文件 | 作用 | 能否本地跑 |
|---|---|---|
| `scripts/ci_local.py` | 本地编排器：真跑测试、切分片、聚合 JUnit、判门禁 | ✅ |
| `gitee-go/pipeline.yml` | Gitee Go 流水线定义（本仓库托管在 Gitee） | 需推送到 Gitee |
| `github-actions/ci.yml` | GitHub Actions 等价实现，用于对照 | 需推送到 GitHub |

`ci_local.py` 不是"玩具版"：它就是这套流水线的可执行规格。YAML 里的每一步，都能在本地用一条命令复现。

---

## 2. 运行

```bash
cd labs/05-ci

python scripts/ci_local.py                              # 快轨：unit + api + ui
python scripts/ci_local.py --shards 2                   # 每个套件切 2 片并行
python scripts/ci_local.py --full                       # 含 slow 标记的全量
python scripts/ci_local.py --with-perf                  # 追加性能门禁
python scripts/ci_local.py --budget 60                  # 墙面耗时预算，超时即失败
python scripts/ci_local.py --suites api --shards 4      # 只跑接口层，切 4 片
```

实测输出（`--shards 2 --with-perf`，数字随机器负载浮动）：

```
== 快轨：unit, api, ui，分片 2 ==
== 性能门禁 ==
  perf: PASS  门禁通过。

== 结果 ==
  [PASS] api#1  用例 12  失败 0  耗时 1.7s  files=test_contract.py,test_state_machine.py
  [PASS] api#2  用例 23  失败 0  耗时 1.8s  files=test_orders_api.py
  [PASS] ui#1   用例  8  失败 0  耗时 4.3s  files=test_checkout_flow.py
  [PASS] unit#1 用例 29  失败 0  耗时 0.9s  files=test_pricing.py

墙面耗时 6.3s / 预算 300s
门禁：通过
```

产物：`.ci-results/summary.md`（可直接贴进 PR 评论）+ 每片一份 JUnit XML。

---

## 3. 流水线的结构（三个文件一致）

```
静态检查（秒级）
      │
      ├── 快轨：单元 + 接口（分片并行）  ── 目标 < 5 分钟，每次提交
      │        └ UI 冒烟（仅 smoke 标记）
      │
      └── 慢轨（仅 master）：全量回归（含 slow）+ 性能门禁
               │
               └ 发布准入：读 summary.md，门禁不通过即阻断
```

**四条设计原则**（[07 §6](../../docs/07-engineering-and-ci.md)）：

1. **双轨**：PR 只跑快轨，避免"评审等 40 分钟"导致没人愿意提 PR；
2. **快速失败**：静态检查不过，后面的测试一秒都不该跑；
3. **统一口径**：所有层都产出 JUnit XML，聚合逻辑只有一处；
4. **门禁 ≤ 3 条**：快轨全绿、慢轨全绿、性能不退化。门禁越多，绕过文化越强。

---

## 4. 分片策略与取舍

本实验用**按文件轮转**切分（`files[i::shards]`）：简单、可复现、无状态。

| 策略 | 优点 | 缺点 | 何时用 |
|---|---|---|---|
| 按文件轮转 | 实现简单、结果稳定 | 分片不均（本例 api 两片 12 vs 23 条） | 起步阶段 |
| 按历史耗时 | 墙面耗时最短 | 需要历史数据存储 | 用例上千条 |
| 按标签（smoke / 模块） | 语义清晰，便于分层 | 需要标签纪律 | 分层门禁 |

> 本例刻意保留了 `12 vs 23` 的不均衡——**这是真实世界的样子**。
> 分片不是银弹，它只把"单机串行"换成"多机并行"，不均衡时收益会打折（[07 §7](../../docs/07-engineering-and-ci.md)）。

---

## 5. 目录

```
05-ci/
├── README.md
├── scripts/
│   └── ci_local.py          # 本地编排器（分层/分片/并行/聚合/门禁）
├── gitee-go/
│   └── pipeline.yml         # Gitee Go 流水线
└── github-actions/
│   └── ci.yml               # GitHub Actions 流水线
```

---

## 6. 常见坑

| 现象 | 原因 | 处理 |
|---|---|---|
| CI 绿但线上挂了 | 重跑掩盖 flaky，或只跑快轨 | 记录重跑次数，把 flaky 率当指标 |
| 分片后反而更慢 | 每片都要装依赖/起服务 | 缓存依赖 + 共享服务（testcontainers 复用） |
| 报告里没有失败现场 | 只收 JUnit，没收日志/截图 | 失败用例的 artifacts 必须归档 |
| 门禁形同虚设 | 规则太多、天天红 | 收敛到 3 条，并把"绕过"纳入度量 |

---

## 7. 延伸练习

- 把 `--shards` 从 2 调到 4，观察墙面耗时变化，思考为什么收益不是线性的。
- 给 `ci_local.py` 加"按上次耗时切分"：把结果写进 `.ci-results/timings.json`，下次按耗时均衡分配。
- 故意让一条用例失败，检查 summary.md 与退出码是否都正确反映（门禁必须可验证）。
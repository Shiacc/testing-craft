# Lab 06 · 平台层：最小可用测试平台（v0.1）

> **一句话**：把 01-04 的实验登记成"资产"，用一张登记表统一调度、统一报告、统一门禁、并留下趋势——
> 这就是"测试平台"的最小骨架，也是理解商业平台在卖什么的最快方式。
>
> 对应文档：[04 测试平台全景](../../docs/04-test-platforms.md)、[01 §4 平台能力模型](../../docs/01-concepts-and-terminology.md)

---

## 1. 它实现了什么，没实现什么

| 能力域（[01 §4.2](../../docs/01-concepts-and-terminology.md)） | v0.1 | 说明 |
|---|---|---|
| 资产管理 | ✅ | `registry/suites.yaml`：套件、标签、负责人、超时、是否可分片 |
| 调度执行 | ✅ | 按标签筛选、按文件分片、线程池并发 |
| 结果与报告 | ✅ | `results.json` + `report.html` + `history.jsonl` |
| 质量门禁 | ✅ | 失败数 / 墙面耗时 / 通过率，退出码驱动 CI |
| 环境管理 | ⚠️ 部分 | 仅区分 `local` / `ci` 两组参数 |
| 设备矩阵 | ❌ | 需要真机云或容器池 |
| 权限与审计 | ❌ | 需要身份体系 |
| 跨团队协作 | ❌ | 需要服务端与多租户 |

> **最后一列是本实验最重要的部分**：它告诉你"什么时候该买/建真平台"。
> 当 ❌ 的三项成为瓶颈时（多团队、多环境、要审计），才值得投入真平台（[04 §7](../../docs/04-test-platforms.md)）。

---

## 2. 运行

```bash
cd labs/06-platform-lite

python -m platform_lite list                 # 看登记了哪些资产、谁负责
python -m platform_lite plan --tag smoke --shards 2   # 只调度不执行，先看切得对不对
python -m platform_lite run --tag smoke --parallel 4 --shards 2
python -m platform_lite run --suite unit,api --env ci
python -m platform_lite history --limit 5
```

实测输出：

```
run_id=20261116-153753-3bd7  环境=local  并行=4  分片=2  标签=smoke
任务数 4，产物目录 ...\runs\20261116-153753-3bd7
  执行完成：4 个任务，墙面耗时 4.1s（并行度 4）

== 结果 ==
  [PASS   ] api#1  用例 12  失败 0   1.63s
  [PASS   ] api#2  用例 23  失败 0   1.68s
  [PASS   ] ui#1   用例  8  失败 0   4.07s
  [PASS   ] unit#1 用例 30  失败 0   0.75s

门禁：通过
报告：...\runs\20261116-153753-3bd7\report.html
```

门禁能拦住（演示用更严阈值）：

```bash
python -m platform_lite run --suite unit --max-duration 0.1
# 门禁：未通过
#   - 耗时 0.7s > 上限 0s
# 退出码 1
```

---

## 3. 四个模块对应平台的四个组件

```
platform_lite/
├── registry.py   资产管理：读 suites.yaml，校验、解析路径（启动即失败）
├── runner.py     调度 + 执行 + 门禁：plan() 切任务、dispatch() 并发、evaluate() 判定
├── report.py     结果存储 + HTML 报告 + 历史趋势（results.json / report.html / history.jsonl）
└── cli.py        统一入口：list / plan / run / history
```

`runs/<run_id>/` 内含：`junit/*.xml`（原始证据）、`results.json`（机器读）、`report.html`（人读）。

---

## 4. 三个设计决定（都有教学意图）

1. **登记表先于一切**：平台不扫描目录，只认 `suites.yaml`。
   这迫使团队回答"这个套件谁负责、超时多少、能不能分片"——这正是"资产管理"的实质。
2. **`plan` 与 `run` 分离**：先看切出来的任务，再决定是否执行。
   真实事故里有一类就是"以为只跑了 smoke，实际跑了全量"。
3. **门禁由退出码表达**：平台把结论交给 CI，而不是靠人看 HTML。
   报告是给人排障的，退出码是给机器放行的。

---

## 5. 常见坑

| 现象 | 原因 | 处理 |
|---|---|---|
| 套件全部 FAIL 但用例明明能过 | 命令里传的是文件名，pytest 需要相对 cwd 的路径 | 本实验用 `tests/test_*.py` 相对路径，见 `runner.discover_test_files` |
| 分片后用例数变少 | 分片把文件切走了但没把用例数汇总 | 按 JUnit 汇总，不按进程数 |
| 门禁"总是通过" | 只判退出码，没判耗时/通过率 | `evaluate()` 里三条件都要 |
| 报告看不懂 | 只有汇总没有明细 | 保留 JUnit 原件，报告只做索引 |

---

## 6. 和真实平台的差距（诚实清单）

- **没有服务端**：没有队列、没有 agent 心跳、没有跨机调度；并发上限就是你本机的核数。
- **没有多租户与权限**：任何人可改登记表；没有审计日志。
- **没有环境/设备池**：环境就是一组额外参数。
- **没有历史数据治理**：`history.jsonl` 会一直增长，没有归档与清理策略。

这四条恰好对应商业平台的主要成本项。**理解这四条，就能判断一份平台方案是在解决真问题还是在卖概念**（[04 §8 五种失败模式](../../docs/04-test-platforms.md)）。

---

## 7. 延伸练习

- 给 `registry.py` 加"套件依赖"（如 ui 依赖 api 通过），实现 `depends_on` 与跳过语义。
- 给 `report.py` 加"近 7 天 flaky 用例 Top10"（同一用例在不同 run 中时红时绿）。
- 把 `runner.execute` 换成"提交到远端 agent"（一个 HTTP 端点），你就迈出了从单机到分布式的第一步。
# 13 · 用例库与缺陷联动

> **适用读者**：QA 负责人、测试工程师、研发效能工程师　|　**前置阅读**：[08](08-metrics-and-quality-gates.md)、[12](12-real-project-playbook.md)
>
> 本文讲两件容易被做成"形式主义"的事：**用例库**与**缺陷自动上报**。
> 关键不在工具，而在**绑定关系是否真实**——绑不住，一切都是装饰。

---

## 1. 用例库：为什么要 YAML，而不是 Excel / 平台里点出来的

| 形态 | 能不能 diff | 能不能进 CI | 能不能被测试代码引用 | 结论 |
|---|---|---|---|---|
| Excel | ❌ | ❌ | ❌ | 只能给人看，半年后成僵尸 |
| 测试平台里点出来 | 部分 | 通过 API | 弱 | 依赖平台，迁移成本高 |
| **仓库内 YAML** | ✅ | ✅ | ✅ | 本仓库选择 |

用例库在本仓库的位置：`atf/cases/cases.yaml`，由 `atf/catalog.py` 加载与校验。

### 1.1 字段规范

| 字段 | 必填 | 说明 |
|---|---|---|
| `id` | ✅ | 稳定标识（`L3-004`）；**改 ID 等于换一条用例**，历史全部作废 |
| `title` | ✅ | 一句话说清"在什么条件下验证什么" |
| `layer` | ✅ | `L1`~`L5`，决定"该在哪一层证明" |
| `priority` | ✅ | `P0`/`P1`/`P2`，决定执行频率与缺陷严重级 |
| `status` | ✅ | `automated` / `manual` / `deprecated` |
| `owner` | ✅ | 谁负责维护（无主用例是腐烂的开始） |
| `automated_by` | 条件 | `status=automated` 时必填，指向真实测试 nodeid |
| `requirement` | 建议 | 对应的功能点/风险，用于追溯 |
| `tags` | 可选 | 执行筛选（smoke / seo / admin…） |
| `last_run` / `last_result` | 自动 | 由执行回写，**不要手填** |
| `notes` | 可选 | 为什么手工、为什么弃用 |

### 1.2 状态机（只有三个态）

```
manual ──(写出并绑定自动化)──▶ automated ──(需求消失/被替代)──▶ deprecated
   ▲                                     │
   └──────(绑定失效：测试被删/改名)────────┘   ← 由 validate 强制回退，不允许悬空
```

**没有 `flaky` 状态**：flaky 是 tag + 隔离期限（≤ 7 天），不是用例的一生。
状态越多，越没人认真维护（见 [09 §4](09-organization-and-governance.md)）。

### 1.3 绑定校验：这是整套东西的地基

```bash
python -m atf validate
```

它做三件事：

1. 字段/枚举/ID 唯一性检查；
2. **把 `automated_by` 拿去和真实测试清单比对**（`pytest --collect-only`）；
3. 列出**黑户测试**——有测试代码但没有用例库条目。

实测输出（本仓库）：

```
用例总数：13
结论：通过（WARN 0 项）
```

> 反过来想：如果这一步不做，`automated_by` 就是一段没人核对的字符串，
> "覆盖率 100%" 只是自欺——这正是绝大多数团队的现状。

### 1.4 覆盖率口径（固定，不允许各算各的）

```
自动化率 = 绑定有效且 status=automated 的用例数 ÷ 用例总数
```

实测（本仓库）：

| 维度 | 结果 |
|---|---|
| 总自动化率 | 92%（12/13） |
| L1 / L3 / L4 | 100% / 100% / 100% |
| L5 | 0%（1 条手工：本地开发服务器不代表生产性能） |
| P0 / P1 / P2 | 100% / 100% / 75% |

`L5=0%` 是**刻意留白**：把本地 dev server 的时延自动化，只会得到一份自我安慰的数字。

### 1.5 结果回写：让用例库"活着"

`python -m atf run` 跑完会把 `last_run` / `last_result` 写回 YAML：

```yaml
- id: L4-002
  last_run: '2026-11-16 19:34:29'
  last_result: pass
```

于是"季度巡检"有了客观依据：

| 判据 | 动作 |
|---|---|
| `last_result=pass` 且连续 2 次季度巡检未变 | 留 |
| `last_result` 长期空 | 查是否从未执行（假覆盖） |
| 对应代码/页面已删除 | **删**（连同测试） |
| 近 7 天失败 ≥ 3 次且原因各异 | 标 `flaky` + 隔离，限期修 |

---

## 2. 缺陷联动：失败之后自动发生什么

```
pytest 失败
   ↓  JUnit XML
atf.runner 解析 → 绑定用例 ID（无 ID 的失败不进缺陷流）
   ↓
atf.bugflow 构造 BugDraft（标题/严重级/步骤/期望/实际/证据）
   ↓
指纹去重（同一用例同一失败签名只提一次）
   ↓
dry-run 打印  ／  真实提交到 禅道 / Jira
```

### 2.1 为什么"没有用例 ID 的失败不进缺陷流"

缺陷的价值在于**能被回归**：修完之后要有一条用例能证明它不再出现。
黑户测试（没绑定用例）提的缺陷，修完没人知道该跑哪条——这种缺陷单迟早变成僵尸。
所以本流程直接拒绝，并在报告里列出这些测试，倒逼先补用例。

### 2.2 缺陷标题规范

```
[层次] 用例标题（用例 ID）
例：[L3] 站点应提供 robots.txt（L3-901）
```

层次前缀让研发一眼知道"这是页面挂了吗"还是"版式错了"，用例 ID 让缺陷可反查。

### 2.3 禅道字段映射

| 本仓库字段 | 禅道字段 | 说明 |
|---|---|---|
| `title` | `title` | 已按 §2.2 规范生成 |
| `severity` | `severity` | P0→严重、P1→一般、P2→轻微（本地口径可改） |
| `priority` | `pri` | 1-4，P0→1 |
| `steps` + `expected` + `actual` | `steps` 多行文本 | 拼成"步骤/期望/实际"三段 |
| `environment` | `os`/`browser` 或备注 | 记录被测地址与执行环境 |
| `case_id` | 备注或自定义字段 | 便于回归反查 |
| 附件 | `files` | 截图/trace（本实验仅记录路径，上传需真实接口） |

认证：优先 `Token: <token>`；否则 `account/password` 换 token。凭据全部走环境变量：

```bash
ZENTAO_BASE_URL=https://zentao.example.com
ZENTAO_PRODUCT_ID=12
ZENTAO_TOKEN=xxxx            # 或 ZENTAO_ACCOUNT / ZENTAO_PASSWORD
```

### 2.4 Jira 字段映射

| 本仓库字段 | Jira 字段 | 说明 |
|---|---|---|
| `title` | `fields.summary` | 同上规范 |
| `priority` | `fields.priority.name` | 1→Highest、2→High、3→Medium、4→Low |
| `description` | `fields.description` | **ADF**（Atlassian Document Format），不是纯文本 |
| `labels` | `fields.labels` | 如 `l3`、`automation` |
| 项目/类型 | `fields.project.key` / `fields.issuetype.name` | 由环境变量与构造参数决定 |

> Jira 的 `description` 用纯文本会报 400。必须给 ADF 的 `doc/paragraph/content` 结构，
> 代码块用 `codeBlock`——这是接入 Jira 最常见的第一个坑。

### 2.5 指纹去重：宁可漏合并，不可误合并

```
fingerprint = sha256(case_id | test_id | 归一化后的 actual)
```

归一化**只抹噪声**：时间戳、UUID、长十六进制、耗时、临时路径、端口。
**刻意不抹数字**——否则"实得 90"和"实得 200"会合并成一个缺陷，那比重提十条更危险。

去重表：`atf/state/filed_bugs.json`（原子写、坏文件隔离、进程内锁）。
CI 多分片并发时，代价只是"偶发重复提单一次"，比"缓存导致后续全部重复提单"轻得多。

### 2.6 dry-run 约定（重要）

没有真实凭据时，流程**必须**停在 dry-run 并如实标注，不允许假装成功：

```bash
$ python -m atf run --tag canary --canary --dry-run
  [would-file] L3-901  标题：[L3] 站点应提供 robots.txt（L3-901）｜严重级：一般
```

有凭据时（本实验用 mock 服务验证）：

```bash
$ python -m atf file-bugs --tracker zentao
用例       动作          系统      单号         说明
L3-901   filed       zentao  101

$ python -m atf file-bugs --tracker zentao     # 再跑一次
L3-901   duplicate   zentao  101        同一指纹已提过，跳过
```

---

## 3. 无凭据时怎么验证（本仓库的做法）

| 手段 | 验证了什么 | 不能验证什么 |
|---|---|---|
| `mock_server.py`（标准库实现禅道/Jira 端点） | 请求体字段、认证头、ADF 结构、4xx 分支、去重 | 真实实例的字段差异、权限模型 |
| 指纹单测 | 归一化稳定性、区分度 | 真实失败文本的多样性 |
| dry-run | 标题/严重级/描述拼装 | 真实提交结果 |

**必须说清的一句**：mock 全绿 ≠ 真实禅道已打通。真实接入前要
`GET` 一次元数据（字段名/必填项/枚举值），再构造 payload——版本差异是常态。

---

## 4. 三个反模式

| 反模式 | 症状 | 修正 |
|---|---|---|
| 用例库与测试代码两套真相 | 改了测试没改用例库，覆盖率虚高 | `validate` 进 CI，绑定失效即红 |
| 失败就自动建单 | 一个环境问题刷出 50 个缺陷 | 指纹去重 + 环境问题先标注再跳过 |
| 用"提了多少缺陷"考核 | 有人开始拆细缺陷刷量 | 只看逃逸缺陷与回归闭环率（[08 §3](08-metrics-and-quality-gates.md)） |

---

## 5. 自检

- [ ] 你们的用例库里，有多少条 `automated_by` 是真的能跑通的？上次校验是什么时候？
- [ ] 一条测试失败后，从"红"到"有人知道"要多久？中间靠人还是靠流程？
- [ ] 如果把指纹里的数字也归一化，会发生什么？为什么那更危险？
- [ ] 没有真实缺陷系统凭据时，你们的流程会"假装成功"吗？

> 下一章：[14 Skill 开发](14-skill-development.md) —— 把上面的流程固化成 AI 可复用的能力。
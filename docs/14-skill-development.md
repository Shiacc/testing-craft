# 14 · Skill 开发：把流程固化成 AI 可复用的能力

> **适用读者**：要把团队流程沉淀给 AI 的工程师、QA 负责人　|　**前置阅读**：[12](12-real-project-playbook.md)、[13](13-case-library-and-bug-integration.md)
>
> 本仓库的 `skills/qa-full-cycle` 是这条流程的可执行封装。本文讲**为什么这么做、标准是什么、怎么校验**。

---

## 1. 为什么把流程做成 Skill，而不是写一份长文档

| 形态 | AI 用起来的成本 | 漂移风险 |
|---|---|---|
| 一份 500 行的《测试流程规范.docx》 | 要么全塞进上下文（贵），要么截断（丢关键） | 高：没人改，改了不起作用 |
| 口头约定 + 散落脚本 | 每次都得重新解释 | 极高 |
| **Skill（骨架 + 按需加载 + 可执行脚本）** | 先读 134 行骨架，需要细节再读 references | 低：引用失效时校验脚本会红 |

核心判断：**Skill 不是"更长的 prompt"，而是"分层披露 + 可校验的产物"**。
它的价值在于：模型第一次读就够用，遇到细节知道去哪里找，跑完能自证。

---

## 2. 标准（唯一真相源，别凭记忆写）

一个 Skill = 一个文件夹，必须有 `SKILL.md`：

```
qa-full-cycle/
├── SKILL.md          # 必需：YAML frontmatter + Markdown 指令
├── references/       # 可选：长篇参考，按需加载
├── scripts/          # 可选：可执行脚本
└── assets/           # 可选：模板（用例模板、缺陷模板）
```

`SKILL.md` frontmatter 硬要求：

| 字段 | 约束 | 本仓库实际值 |
|---|---|---|
| `name` | 1–64 字符；小写字母/数字/连字符；**必须与文件夹同名** | `qa-full-cycle` |
| `description` | 1–1024 字符；必须同时说明"做什么 + 何时用 + 触发词" | 191 字符，含 5 类触发场景 |

> 反例：目录叫 `qa_full_cycle`、name 写 `qa-full-cycle` → 加载器按目录名索引时对不上，技能"看不见"。

---

## 3. 分层披露：每一步放哪里

| 层 | 放什么 | 本仓库实体 | 行数 |
|---|---|---|---|
| 骨架 | 铁律、五步流程、入口指针、产物清单 | `SKILL.md` | 134 |
| 参考 | 全流程细节、字段规范、字段映射 | `references/workflow.md` / `case-library-spec.md` / `bug-integration.md` | 289 / 190 / 257 |
| 执行 | 体检、一键跑 | `scripts/check_env.py` / `run_cycle.py` | 287 / 276 |
| 模板 | 用例条目、缺陷描述 | `assets/case-template.yaml` / `bug-template.md` | 50 / 71 |

**判断标准**：SKILL.md ≤ 500 行，且"只读 SKILL.md 就能开始干活"。做不到说明骨架写歪了。

---

## 4. 三条铁律（写进 SKILL.md 的那部分）

| 铁律 | 为什么必须写进骨架 |
|---|---|
| **无凭据必须 dry-run 并标注"未验证"** | AI 最容易犯的错是"看起来很成功"。没有凭据就是没有，不许假装 |
| **失败必须能追溯到用例 ID** | 没有 ID 的失败无法回归，提了缺陷也修不干净 |
| **不得为变绿改基线/改断言** | 这是测试体系里最危险的动作，且极容易被"优化"成习惯 |

> 铁律只放**可长期复用的判据**。具体踩坑（比如 wagtail 的 slug 要 blur）进 references——
> 否则规则会膨胀成日志，最后没人读。

---

## 5. 校验：全 PASS 才算完成

```bash
python <skill-creator>/scripts/validate_skill.py skills/qa-full-cycle
```

实测输出：

```
  PASS SKILL.md     存在
  PASS frontmatter  2 个字段
  PASS name         qa-full-cycle
  PASS name=文件夹     qa-full-cycle
  PASS description  191 字符，含使用场景
  PASS 目录结构         符合 scripts/references/assets 约定
  PASS 篇幅           134 行
  PASS 引用完整         7 个引用均可解析
  PASS 脚本语法         2 个 .py 通过
共 1 个 skill，1 个无 FAIL，0 个有 FAIL
```

**WARN 不许当 PASS 报**。校验脚本也不许按现状放宽——那比不校验更糟。

---

## 6. 脚本必须真的能跑（否则 Skill 是幻灯片）

本仓库实测链路：

```bash
$ python skills/qa-full-cycle/scripts/check_env.py --target-url http://127.0.0.1:8099
[OK  ] Python 版本   3.12.10
[OK  ] pytest        9.2.0
[OK  ] atf           可调用（python -m atf）
[OK  ] 被测项目在线  http://127.0.0.1:8099 → HTTP 200
结论: 通过 退出码 0

$ python skills/qa-full-cycle/scripts/run_cycle.py --tag "not canary" --target-url http://127.0.0.1:8099
[1/3] 环境体检 … [2/3] 探测 atf … [3/3] 执行
结论: 执行成功（atf 退出码 0），耗时 9.7s
```

两个细节值得学：

1. **atf 缺失时退出码 3，并先打印"本来要跑的命令"再解释**——让人看到意图，但不给"跑过了"的错觉；
2. **`--dry-run` 明确声明"不产生任何结论"**，避免被用来写周报。

---

## 7. 与 atf 的分工（别混）

| 层 | 角色 | 产物 |
|---|---|---|
| Skill | **操作手册 + 编排**：告诉 AI/人"先做什么、什么算做完、失败怎么回退" | 决策路径、命令序列 |
| atf | **执行器**：用例库读写、跑测试、回写结果、生成缺陷草稿 | YAML 变更、JUnit、summary.md、缺陷单 |

Skill 不含业务逻辑，atf 不含流程判断——这样换流程不必改代码，换实现不必改文档。

---

## 8. 五个反模式

| 反模式 | 后果 | 正确做法 |
|---|---|---|
| 只有 SKILL.md，几百行全塞进去 | 上下文一次占满，改一处要看全部 | 骨架 + references 分层 |
| 目录叫 `rules/`、`tools/` | 跨产品不可移植 | `references/` / `scripts/` |
| `name` 与文件夹不一致 | 技能加载不到 | 完全一致 |
| description 只写"做什么" | 模型不知道何时触发 | 补触发场景与关键词 |
| 把我这次踩的坑写进铁律 | 规则膨胀成日志 | 坑进 references，铁律只留判据 |

---

## 9. 自检

- [ ] 你们的流程文档，有没有"骨架 ≤ 500 行、细节按需加载"的分层？
- [ ] 描述里写清"何时用"了吗？把它单独发给一个没上下文的人，他能判断该不该用吗？
- [ ] 校验脚本是"产物合规"还是"脚本能跑"？两者都要。
- [ ] Skill 里的每条铁律，都是可长期复用的判据吗？还是只是上次事故的回忆？

> 回到索引：[docs/index.md](index.md)
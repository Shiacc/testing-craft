# 15 · 配置与操作手册（Runbook）

> **适用读者**：要自己跑一遍、或把这套流程搬到另一个项目的人　|　**前置阅读**：[12](12-real-project-playbook.md)、[13](13-case-library-and-bug-integration.md)、[14](14-skill-development.md)
>
> 一句话：**一条命令，从"删库"跑到"缺陷单"，产出可审计的报告。** 本手册把所有配置项、操作方式、故障处置写全。

---

## 1. 全流程一览

```
┌──────────────────────────────────────────────────────────────────────┐
│  ① 重置环境      删 sqlite + 种子状态 → migrate → seed → 建账号 → 清理 │
├──────────────────────────────────────────────────────────────────────┤
│  ② 起被测服务    按配置起 runserver → 轮询就绪（不 sleep 猜）          │
├──────────────────────────────────────────────────────────────────────┤
│  ③ 分层执行      l1 → l3 → l4 → canary（每层独立 JUnit + 控制台日志）  │
├──────────────────────────────────────────────────────────────────────┤
│  ④ 归因回写      失败 → 绑定用例 ID → 回写 last_run/last_result        │
├──────────────────────────────────────────────────────────────────────┤
│  ⑤ 缺陷联动      dry-run / mock（禅道+Jira）/ submit（真实凭据）        │
├──────────────────────────────────────────────────────────────────────┤
│  ⑥ 报告归档      RUN.md + results.json + junit + 日志 + 覆盖率快照     │
└──────────────────────────────────────────────────────────────────────┘
```

实测（本仓库，Windows + Python 3.12）：

```
项目 wagtail-demo｜缺陷联动 mock/both｜重置 是
  [PASS         ] l1      pass  3 fail  0    4.7s
  [PASS         ] l3      pass  8 fail  0    5.6s
  [PASS         ] l4      pass  2 fail  0    9.0s
  [EXPECTED_FAIL] canary  pass  0 fail  1    5.0s
  [filed] L3-901 zentao 101
  [filed] L3-901 jira QA-1
结论：通过（退出码 0）｜43.3s
```

---

## 2. 快速开始

前置：Python 3.11+，`playwright` 已装（浏览器可用系统 Chrome/Edge 回落）。

```powershell
# 首次：安装依赖 + 建库 + 灌种子 + 建本地账号（幂等）
cd labs/07-real-project; .\bootstrap.ps1

# 回到仓库根，一条命令跑完整流程
cd <repo>
.\scripts\run_e2e.ps1                                   # 默认 mock：禅道 + Jira 都真实走一遍
```

等价的内核命令（CI / Linux 用这个）：

```bash
python -m atf e2e --filing mock --tracker both
```

---

## 3. 配置项总表（`atf/config/e2e.yaml`）

### 3.1 被测服务 `server`

| 字段 | 默认 | 作用 | 注意 |
|---|---|---|---|
| `cwd` | `labs/07-real-project/app` | 服务启动目录 | 换项目时改这里 |
| `command` | `["{python}","manage.py","runserver","{host}:{port}","--noreload"]` | 启动命令 | `{python}`/`{host}`/`{port}` 运行时替换；`--noreload` 防止双进程 |
| `host` / `port` | `127.0.0.1` / `8099` | 监听地址与端口 | 端口被占用时**直接报错**（不自动改端口，避免连到旧服务） |
| `ready_path` | `/admin/login/` | 就绪探测路径 | 返回 <400 视为就绪 |
| `ready_timeout_s` | `90` | 就绪超时 | 首次启动（migrate 后）会慢，别设太小 |
| `ready_interval_s` | `1.0` | 轮询间隔 | — |
| `log_file` | `server.log` | 服务日志文件名 | 落 `<artifacts>/<run_id>/` |

### 3.2 环境重置 `setup`

| 字段 | 默认 | 作用 |
|---|---|---|
| `reset_files` | `db.sqlite3`、`.seed_state.json` | 每次运行前删除，确保"从零" |
| `commands` | migrate → seed → 建账号 → 清理 | 按顺序执行，任一非 0 即中止（写入 `setup.log`） |

> 关键：**重置是一等公民**。没有它，"能跑通"永远依赖上一轮留下的数据。

### 3.3 分层执行 `suites[]`

| 字段 | 必填 | 作用 |
|---|---|---|
| `id` | ✅ | 套件标识（也是产物文件名的一部分） |
| `name` | | 报告里的可读名 |
| `marker` | ✅ | pytest `-m` 表达式，如 `l4`、`l3 and not canary` |
| `required` | | `true` = 失败即整体不通过 |
| `expect_failures` | | `true` = **预期失败**（演练用缺陷）；失败记 `EXPECTED_FAIL`，不再失败记 `FIXED`（提醒把用例转正） |
| `env` | | 该套件额外环境变量，如 `QA_RUN_CANARY: "1"` |

`suites_cwd`（顶层）指定测试代码所在目录，默认 `labs/07-real-project`。

### 3.4 缺陷联动 `filing`

| 字段 | 取值 | 说明 |
|---|---|---|
| `mode` | `dry-run` / `mock` / `submit` | 只预览 / 走 mock 真实提交 / 提交真实系统 |
| `tracker` | `zentao` / `jira` / `both` | 可同时验证两套适配器 |
| `isolate_state` | `true` | mock 模式下每轮独立去重表，避免上一轮把这一轮判成 duplicate |

`mode` 优先级可由命令行 `--filing` 覆盖。

### 3.5 产物与超时

| 字段 | 默认 | 说明 |
|---|---|---|
| `artifacts.dir` | `atf/e2e-runs` | 运行产物根目录 |
| `keep_junit` / `write_markdown` | `true` | 是否保留 JUnit / 生成 RUN.md |
| `timeouts.suite_s` | `900` | 单个套件超时 |
| `timeouts.setup_s` | `300` | 单条准备命令超时 |
| `timeouts.total_s` | `3600` | 总预算（用于容量规划） |

---

## 4. 环境变量

| 变量 | 用于 | 必需性 |
|---|---|---|
| `ZENTAO_BASE_URL` / `ZENTAO_PRODUCT_ID` / `ZENTAO_TOKEN`（或 `ZENTAO_ACCOUNT`+`ZENTAO_PASSWORD`） | 禅道提交 | `submit` 模式必需 |
| `JIRA_BASE_URL` / `JIRA_EMAIL` / `JIRA_API_TOKEN` / `JIRA_PROJECT_KEY` | Jira 提交 | `submit` 模式必需 |
| `QA_RUN_CANARY` | 启用演练用负向用例 | 由套件配置注入，无需手工设 |
| `QA_BASE_URL` | 覆盖被测地址 | 可选（默认取配置） |
| `PW_CHANNEL` | 指定浏览器通道（chrome/msedge） | 可选 |
| `PYTHONUTF8` | 中文输出 | 脚本已自动设置 |

> 凭据**只走环境变量**，`submit` 模式下缺凭据会以**退出码 3** 明确拒绝，绝不静默降级成 dry-run。

---

## 5. 分步操作（排查与局部验证）

```powershell
# 只预览缺陷流，不联网
python -m atf e2e --filing dry-run

# 只验禅道适配器（mock）
python -m atf e2e --filing mock --tracker zentao

# 保留服务，便于手工点开后台排查
python -m atf e2e --keep-server
# → 服务留在 http://127.0.0.1:8099，处理完手动关掉

# 调试时不想重置数据（跳过删除 sqlite）
python -m atf e2e --no-reset

# 换端口（默认 8099 被占用时）
python -m atf e2e --port 8100

# 验收：连跑 3 次并对比一致性
python -m atf e2e --repeat 3
```

只跑某一层（不走编排）：

```powershell
cd labs/07-real-project
python -m pytest -q -m l3
```

### 5.1 缺陷系统体检（真实提交前必做）

提交前先用**只读**请求确认：项目存在吗？产品存在吗？`Bug` 类型可用吗？

```powershell
python -m atf tracker check --tracker both            # 用环境变量里的真实凭据
python -m atf tracker check --tracker both --mock     # 起本地 mock，验证体检逻辑本身
```

- 通过：三行全 `OK`，退出码 `0`，此时才值得试 `--filing submit`；
- 未通过：退出码 `3`，并直接告诉你缺哪些环境变量或哪个项目/产品不存在。

> 为什么必须先 GET：禅道/Jira 的字段名、必填项、枚举值随版本与实例配置变化。
> 直接 POST 的代价是"提单 400，才发现项目 key 拼错"。

---

## 6. 多项目：一套内核，多个项目

登记表：`atf/projects.yaml`

```yaml
default: wagtail-demo
projects:
  wagtail-demo:
    config: atf/config/e2e.yaml
    cases: atf/cases/cases.yaml
    suites_cwd: labs/07-real-project
  static-demo:
    config: atf/config/e2e-static.yaml
    cases: atf/cases/static-cases.yaml
    suites_cwd: tests/static-demo
```

```powershell
python -m atf e2e --project static-demo     # 跑第二个项目（3s，静态站点）
python -m atf validate --project static-demo
python -m atf coverage --project static-demo
```

三个约定（照做即可，不需要改内核）：

| 约定 | 说明 |
|---|---|
| `suites_cwd` 是**相对路径基准** | 用例库里的 `automated_by` 写"相对该目录"的 nodeid；`collect_tests` 也在该目录执行 |
| 产物按项目分目录 | `atf/e2e-runs/<project>/<run_id>/`，两个项目互不覆盖 |
| 项目间不共享用例库 | 每个项目一份 `cases.yaml`，覆盖率各算各的 |

本项目已内置两个项目作为对照：`wagtail-demo`（真实全栈，4 层 + 缺陷联动）与 `static-demo`
（静态站点，1 层 + dry-run）。后者证明"新增项目 = 加配置 + 加测试目录"。

---

## 7. 产物清单

`atf/e2e-runs/<run_id>/`：

| 文件 | 内容 | 谁看 |
|---|---|---|
| `RUN.md` | 结论 + 分层表 + 失败明细 + 缺陷流 + 覆盖率 | 负责人 |
| `results.json` | 同上的机器可读版（含签名，用于对比） | CI / 看板 |
| `junit-<suite>.xml` | 每层原始结果 | 工程师、CI |
| `console-<suite>.log` | pytest 控制台输出 | 排障 |
| `setup.log` | 重置与准备命令的完整输出 | 排障（环境问题先看它） |
| `server.log` | 被测服务日志 | 排障（500/异常堆栈在这里） |
| `state-<tracker>.json` | mock 模式的去重表 | 审计"提了什么单" |

---

## 8. 验收标准（"一次性完整跑通"的定义）

不是"我跑了一次没报错"，而是这四条同时成立：

| # | 判据 | 命令 | 期望 |
|---|---|---|---|
| 1 | **从零可跑**：删库重建后仍通过 | 先删 `db.sqlite3` 与 `.seed_state.json`，再 `run_e2e.ps1` | 退出码 0 |
| 2 | **可重复**：连跑 3 次结果一致 | `--repeat 3` | 三次签名完全相同，退出码 0 |
| 3 | **可追溯**：失败能落到用例 ID | 看 `RUN.md` 失败明细 | 每行都有 `L*-***` |
| 4 | **缺陷闭环**：失败能生成缺陷 | `--filing mock` | `[filed] ... zentao 101` / `jira QA-1` |

`--repeat` 的对比信号（`signature`）只包含**应该稳定**的部分：每层状态与计数、覆盖率、提交动作。
时间、run_id、时间戳不参与比较——否则每次都会被判"不一致"。

---

## 9. 换一个项目怎么复用

这套流程对"任何能在本机起服务的项目"都适用，改动点固定为 6 处：

| # | 改什么 | 位置 | 说明 |
|---|---|---|---|
| 1 | 服务启动命令/端口/就绪路径 | `server.*` | 就绪路径要选一个**不依赖登录**的页面 |
| 2 | 重置动作 | `setup.reset_files` / `setup.commands` | 数据库/缓存/上传目录；顺序敏感（先删后建） |
| 3 | 分层与 marker | `suites[]` + `suites_cwd` | marker 必须与测试代码里的 `pytestmark` 一致 |
| 4 | 用例库 | `atf/cases/cases.yaml` | `automated_by` 必须指向真实 nodeid，`atf validate` 会校验 |
| 5 | 缺陷字段映射 | `atf/bugfiler/*.py` | 禅道字段名、Jira 项目 Key 随实例不同 |
| 6 | 演练用负向用例（可选） | 测试代码 + `canary` 套件 | 没有真实缺陷可练时，用它验证上报链路 |

接入检查表（照做即可）：

- [ ] `bootstrap` 类脚本能从零装好依赖并初始化数据（幂等）
- [ ] `python -m atf validate` 0 error
- [ ] `python -m atf e2e --filing dry-run` 退出码 0
- [ ] `--repeat 3` 三次一致
- [ ] `RUN.md` 里失败项都有用例 ID
- [ ] 跑完无残留（进程、`qa-` 数据、临时文件）

---

## 10. 故障排查

| 症状 | 原因 | 处理 |
|---|---|---|
| `端口 8099 已被占用` | 上次服务没退（`--keep-server` 或崩溃遗留） | 结束占用进程，或 `--port 8100` |
| 服务启动即退出 | 依赖缺失/迁移未跑/配置错 | 看 `server.log` 与 `setup.log` 末尾 |
| 就绪超时但进程还活着 | 就绪路径本身 500 | 手工 `curl` 该路径确认；必要时换 `ready_path` |
| 全部用例连接失败 | `QA_BASE_URL` 与实际端口不一致 | 检查 `server.port` 与 `--port` |
| `atf validate` 报"绑定的测试不存在" | 改了测试函数名/路径，或 `suites_cwd` 写错 | 同步 `automated_by`（相对 `suites_cwd`）；这是设计上的保护，不要绕过 |
| 换项目后所有用例 0 条 | marker 与测试里的 `pytestmark` 不一致 | 用 `pytest --collect-only -q -m <marker>` 自查 |
| `submit` 模式退出码 3 | 凭据缺失 | 按 §4 配环境变量；缺凭据是**拒绝执行**，不是失败 |
| 多次运行结果不一致（`--repeat` 失败） | 数据残留 / 端口/账号共享 / 断言含时间或随机 | 用 `reset_files` 覆盖干净；断言去掉时间戳；看 `setup.log` 是否每次一致 |
| 中文乱码 | 控制台编码 | 脚本已设 `PYTHONUTF8=1`；手工跑时加 `-X utf8` |
| 浏览器启动失败 | 无 chromium 且无系统浏览器 | 装 Chrome/Edge，或 `PW_CHANNEL=msedge` |
| mock 缺陷服务起不来 | 端口被占 / 模块路径错 | `python -m atf.bugfiler.mock_server --port 0` 手工验证 |
| `canary` 变成 `FIXED` | 被测系统的缺陷被修了 | 把它转成正式回归用例、更新基线，再从 canary 移除 |
| 报告里出现"黑户测试" | 有测试无用例条目 | 补用例库条目（`atf validate` 会给警告） |

---

## 11. CI 集成

退出码语义（可直接作为流水线判据）：

| 退出码 | 含义 | 流水线动作 |
|---|---|---|
| `0` | 通过（含预期的 canary 失败） | 继续 |
| `1` | 用例失败 / 多次运行不一致 | 阻断，附 `RUN.md` |
| `2` | 配置错误（e2e.yaml 缺字段等） | 阻断，修配置（不要重试） |
| `3` | 环境/凭据未就绪（端口占用、缺凭据、服务起不来） | 阻断，通知环境负责人 |

本仓库已内置两份流水线文件（意图一致，便于跨平台迁移）：

### 11.0 已验证的 GitHub 运行（2026-11-17，Linux runner 实测）

仓库：`git@github.com:Shiacc/testing-craft.git`（公开，Actions 免费不限量）

现状：**run #1–#4 全部 success**，且每次 push 的 `submit` job 都是 `skipped`（闸门有效）。

以下是 run #4（`1af0552`）端到端 job 的**真实日志摘录**（Linux runner 比本机快，26s 跑完项目 1）：

```
项目 Wagtail 全栈 CMS（wagtail-demo）｜缺陷联动 mock/both｜重置 是
  [PASS         ] l1      pass  3 fail  0    2.5s
  [PASS         ] l3      pass  8 fail  0    2.6s
  [PASS         ] l4      pass  2 fail  0    5.5s
  [EXPECTED_FAIL] canary  pass  0 fail  1    2.3s
  [filed] L3-901 zentao 101
  [filed] L3-901 jira QA-1
结论：通过（退出码 0）｜26.0s
项目 静态站点（static-demo）｜缺陷联动 dry-run/both｜重置 是
  [PASS         ] static  pass  3 fail  0    0.4s
结论：通过（退出码 0）｜1.3s
```

关键点（都可对着日志逐条核对）：

| 观察 | 含义 |
|---|---|
| l1 3 / l3 8 / l4 2 全 `PASS` | 三层用例在干净 Linux runner 上可复现 |
| canary `EXPECTED_FAIL` | 演练用负向用例按预期失败，且**不计入**整体失败 |
| `[filed] zentao 101` / `[filed] jira QA-1` | 缺陷流在 CI 里真实走了 mock 提交（两套适配器都通） |
| 项目 2 独立 1.3s | 多项目复用只需换配置，启动开销极小 |
| submit job `skipped` | push 事件不会向真实缺陷系统提单 |

### 11.1 先上 GitHub（免费额度说明 + 三步接入）

| 仓库可见性 | Actions 免费额度 | 本项目单次耗时 | 结论 |
|---|---|---|---|
| **public** | **不限量**（标准 runner） | 约 3-5 分钟（static + 两个项目 + repeat 3） | 推荐 |
| private | 2000 分钟/月（Linux 1×） | 同上 | 够用但要盯配额 |

接入三步（本仓库提供脚本，避免手点出错）：

```powershell
# 1) 准备 PAT（fine-grained，仅限本仓库）需要的权限：
#    Contents: Read and write      —— 推送代码
#    Workflows: Read and write     —— 提交/更新 .github/workflows
#    Administration: Read and write —— 创建仓库（仓库已存在则不需要）
# 2) 把 token 存成文件（不要贴进聊天、不要提交进仓库）
#    例如保存到 <workspace>\.tmp\gh-token.txt
# 3) 一条命令：建仓 → 推送 → 触发 Actions → 等待结果
python scripts/gh_publish.py --token-file ..\.tmp\gh-token.txt --wait
```

### 11.2 不想配 SSH 时：一次性 PAT 推送

如果 SSH 公钥在你的 GitHub 上一直不生效，用这个脚本在**你自己的终端**里推一次即可
（token 以掩码输入、只在内存活一次，不落盘、不进 `git config`、不进命令行历史）：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\gh_push_once.ps1
# 只看计划：-DryRun
```

它做的就是 `git push https://github.com/<repo>.git master:main`，鉴权走 `http.extraheader`。
PAT 需要 `Contents: Read and write` + `Workflows: Read and write`（仓库含 `.github/workflows/`，缺后者会被拒）。
推完 Actions 自动触发，地址：`https://github.com/<owner>/<repo>/actions`。

脚本做的事与安全约定：

- token 只从文件/环境变量读，打印时脱敏（`ghp_***（len=32）`）；
- 推送用 `http.extraheader` 传凭据，**不写进 git remote URL**，结束后 remote 只留干净的 HTTPS 地址；
- 建仓默认 **public**；`--private` 可切私有；
- `--wait` 会轮询 Actions 并把结论（success/failure + run URL）打印出来，退出码 0/1；
- `--dry-run` 只打印计划，不调 API、不推送（用于确认参数）。

> 首次推送建议用 `--wait`：能当场看到 Actions 通过/失败，而不是过一会儿才发现 workflow 写错。

两套都只调一条命令，编排逻辑留在内核：

```yaml
# PR 门禁：快（单次）
- run: python -m atf e2e --project wagtail-demo --filing mock --tracker both
- run: python -m atf e2e --project static-demo
# 主干验收：连跑 3 次一致
- run: python -m atf e2e --project wagtail-demo --filing dry-run --tracker both --repeat 3
```

### 11.3 在 CI 里真实提单（GitHub Secrets + 手工触发）

日常 push/PR **只跑 mock 与 dry-run**，真实提单只允许手工触发，避免把缺陷系统写成噪音源。

**第一步：配 Secrets**（仓库 Settings → Secrets and variables → Actions）

| Secret | 用于 | 示例 |
|---|---|---|
| `ZENTAO_BASE_URL` | 禅道地址 | `https://zentao.example.com` |
| `ZENTAO_PRODUCT_ID` | 目标产品 ID | `12` |
| `ZENTAO_TOKEN` | 禅道 Token（或改用 `ZENTAO_ACCOUNT`/`ZENTAO_PASSWORD`） | `xxxx` |
| `JIRA_BASE_URL` | Jira 地址 | `https://yourco.atlassian.net` |
| `JIRA_EMAIL` | 提单账号邮箱 | `qa@example.com` |
| `JIRA_API_TOKEN` | Jira API Token | `xxxx` |
| `JIRA_PROJECT_KEY` | 项目 Key | `QA` |

**第二步：手工触发**（Actions → qa-e2e → Run workflow）

`real_submit` 选项：`none`（默认，只跑 mock）/ `both` / `zentao` / `jira`。选非 `none` 时才会执行 `submit` job：

1. `atf tracker check --tracker <选择>` —— 缺 Secrets 或字段不匹配就在这里以**退出码 3** 中止；
2. `atf e2e --project wagtail-demo --filing submit --tracker <选择>` —— 真实提单；
3. 归档 `atf/e2e-runs/`（含缺陷单号，保留 30 天）。

**三条安全约定**（已写进 workflow）：

- push/PR 事件永远不会走 `submit`（job 的 `if` 限定 `workflow_dispatch` 且 `real_submit != none`）；
- 缺凭据时明确失败，不静默降级；
- 提单产物保留 30 天，便于审计"提了哪些单"。

> 真实缺陷系统只在发布前用 `--filing submit`（先跑 `atf tracker check`）；日常 PR 用 `mock`/`dry-run`。

> 真实缺陷系统只在发布前用 `--filing submit`；日常 PR 用 `mock` 或 `dry-run`，避免噪声。

---

## 12. 常见误用

| 误用 | 后果 | 正确做法 |
|---|---|---|
| 把 `dry-run` 的结果当"已提交" | 缺陷其实没建，问题无人跟进 | dry-run 只用于预览；真实提交用 `submit` |
| 为让门禁变绿而改基线/删断言 | 测试失去意义 | 改基线必须留记录并说明理由 |
| 跳过 `reset_files` 长期跑 | 结果依赖历史数据，第一次在新机器上必然失败 | 保留重置；需要保留数据时显式 `--no-reset` |
| 在 `submit` 模式共享个人账号 | 无法审计、离职即失效 | 用专用测试账号 + CI Secret |
| 用例库不校验就提交 | `automated_by` 变成装饰 | `atf validate` 进 CI |

---

## 13. 相关文档与技能

| 目标 | 看哪里 |
|---|---|
| 为什么这样设计（真实项目接入） | [12](12-real-project-playbook.md) |
| 用例库字段、缺陷字段映射、指纹去重 | [13](13-case-library-and-bug-integration.md) |
| 把流程做成 AI 可复用的 Skill | [14](14-skill-development.md) |
| 一条命令的执行入口 | `scripts/run_e2e.ps1`、`python -m atf e2e` |
| AI 侧的操作手册 | `skills/qa-full-cycle/`（SKILL.md → references/workflow.md） |
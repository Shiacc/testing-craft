# Lab 07 · 真实开源项目接入（Wagtail 全栈 CMS）

> **一句话**：不是玩具项目、不是 mock 服务——把**真实开源全栈项目**跑起来，
> 在上面跑通 L1→L4 四层测试，再由 `atf` 完成「用例库 ↔ 测试代码 ↔ 缺陷系统」的闭环。

| 项 | 内容 |
|---|---|
| 被测系统 | [Wagtail](https://github.com/wagtail/wagtail) 6.4 / Django 5.2（真实开源 CMS，20k+ star） |
| 选型理由 | 真前后端（Django 服务端渲染 + 后台 SPA）、pip 可跑、无需 Node 构建、业务流完整（创建→发布→前台可见） |
| 访问地址 | http://127.0.0.1:8099（本地 sqlite） |
| 测试账号 | `qa` / `qa-pass-12345`（仅本地库，非生产凭据） |

---

## 1. 为什么选"真实项目"而不是自己造一个

自己造的被测系统会不知不觉地"配合测试"：字段刚好稳定、错误刚好可断言、状态机刚好简单。
用真实开源项目才会遇到真实困难，而本仓库的结论正是在这些困难上成立的：

| 真实困难（本实验遇到） | 处理方式 |
|---|---|
| Wagtail 后台是渐进增强的 SPA，按钮藏在 "More actions" 下拉里 | 用可见性等待（`expect(...).to_be_visible()`），而不是 `click` 硬点 |
| 标题输入后 slug 由前端 blur 时生成，直接提交会校验失败 | 填完标题 → blur → 等 slug 非空再提交 |
| 面板字段名/结构随版本变化（6.x） | 只依赖稳定契约（`#id_title`、`name=action-publish`），不写死布局 |
| 页面 URL 由父页面决定 | 先读种子落盘的 `.seed_state.json` 拿父页面 ID，不猜 |
| UI 测试会留下垃圾数据 | 统一 `qa-` 前缀 + `cleanup_qa_pages` 管理命令 |

---

## 2. 快速开始

```powershell
cd labs/07-real-project
.\bootstrap.ps1                 # 装依赖 → 迁移 → 灌数据 → 建测试账号（幂等）

cd app
python manage.py runserver 127.0.0.1:8099 --noreload   # 终端 A：起被测站
```

```powershell
cd labs/07-real-project
python -m pytest -q             # 终端 B：13 passed，1 skipped（canary，默认跳过）
```

从仓库根跑完整闭环（用例库 + 缺陷流）：

```powershell
python -m atf validate                      # 用例库校验：绑定是否真实存在
python -m atf coverage                      # 覆盖率：分层 / 优先级
python -m atf run --tag "not canary"        # 执行 + 回写用例库 + 出报告
python -m atf run --tag canary --canary --dry-run   # 失败项 → 缺陷草稿（不提交）
```

---

## 3. 四层测试都测了什么

| 文件 | 层 | 断言什么 | 数量 |
|---|---|---|---|
| `tests/test_l1_seed_plan.py` | L1 | 种子计划结构（slug 唯一、标题/摘要非空） | 3 |
| `tests/test_l3_site_http.py` | L3 | 页面 200、主标题、列表子链接、sitemap、静态资源、后台登录页 | 7（含 1 条 canary） |
| `tests/test_l4_admin_ui.py` | L4 | 后台登录；新建页面 → 发布 → 前台可见 | 2 |
| 合计 | | | **13 + 1** |

**L1 测的是"我们自己的胶水代码"**，不是 Wagtail 内部逻辑——框架自身逻辑由上游测试保证，
重复覆盖它既慢又无价值。这条取舍见 [docs/12](../../docs/12-real-project-playbook.md)。

---

## 4. 目录

```
07-real-project/
├── README.md
├── bootstrap.ps1              一键就绪（幂等）
├── requirements.txt
├── pytest.ini                 marker 声明（smoke/l1/l3/l4/canary）
├── app/                       Wagtail 项目（脚手架 + 最小改动）
│   ├── demo/                  配置（sitemaps、CSRF 可信源）
│   └── home/
│       ├── models.py          + StandardPage（通用内容页）
│       ├── templates/home/standard_page.html
│       └── management/commands/
│           ├── seed_demo.py         幂等种子 + 落盘 .seed_state.json
│           ├── cleanup_qa_pages.py  清理 qa- 前缀测试页面
│           └── ensure_qa_user.py    本地测试账号
└── tests/
    ├── conftest.py            被测站探活、Playwright 回落、自动清理
    ├── test_l1_seed_plan.py
    ├── test_l3_site_http.py
    └── test_l4_admin_ui.py
```

---

## 5. 与 atf 的衔接

`atf/cases/cases.yaml` 里每条用例的 `automated_by` 指向本目录的测试 nodeid，例如：

```yaml
- id: L4-002
  title: 后台新建页面并发布后前台可见
  layer: L4
  priority: P0
  status: automated
  automated_by: tests/test_l4_admin_ui.py::test_create_and_publish_page_then_visible_on_site
```

`python -m atf run` 跑完后会把 `last_run` / `last_result` 回写进用例库——
**用例库因此是"活的"**，而不是一份三个月没人更新的 Excel。

---

## 6. 已知限制

| 限制 | 说明 |
|---|---|
| 本地 sqlite + 开发服务器 | 不代表生产性能，L5 性能不在本实验范围内（标为 manual） |
| 后台 UI 依赖 Wagtail 6.x 结构 | 升级大版本时需重跑一次 L4，失败属预期（结构变了） |
| `robots.txt` 缺失 | 这是被测项目的真实缺口，作为 canary 用例验证缺陷上报链路 |
| 未接真实禅道/Jira | 端到端用 `atf/bugfiler/mock_server.py` 验证；真实提交需业务方提供凭据 |
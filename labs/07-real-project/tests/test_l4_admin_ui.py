"""L4 端到端：在真实管理后台完成"登录 → 新建页面 → 发布 → 前台可见"。

这是整个仓库里最有说服力的一条用例：
它跨越了**后台 UI + 权限 + 数据库 + 页面发布状态机 + 前台渲染**五层，
任何一环坏掉都会失败——而这正是"自动化测试能替代的重复人工回归"的典型形态。

设计约束：
- 只填 title/intro 两个纯文本字段，不碰富文本编辑器（Draftail 依赖 JS 富交互，属于
  "为了测试而测试"的脆弱点，收益低于维护成本）；
- 标题用 ASCII，slug 由系统自动生成，前台 URL 可确定预测；
- 所有创建的页面 slug 以 qa- 开头，由 conftest 的 cleanup 统一清理。
"""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import expect

pytestmark = [pytest.mark.l4]

PAGE_TITLE = "qa ui acceptance page"
EXPECTED_PATH = "/guide/qa-ui-acceptance-page/"


@pytest.mark.smoke
def test_admin_login_succeeds(page, base_url, credentials, seed_state):
    page.goto(f"{base_url}/admin/login/", wait_until="domcontentloaded")
    page.fill('input[name="username"]', credentials["username"])
    page.fill('input[name="password"]', credentials["password"])
    page.click('button[type="submit"]')

    page.wait_for_url(re.compile(r"/admin/"), timeout=20000)
    # Wagtail 6 后台侧栏是按钮式导航，没有 /admin/pages/ 链接，用可见的导航按钮判定
    expect(page.get_by_role("button", name="Pages")).to_be_visible()


def test_create_and_publish_page_then_visible_on_site(page, base_url, credentials, seed_state, http):
    guide_id = seed_state["pages"]["guide"]

    # 1) 登录
    page.goto(f"{base_url}/admin/login/", wait_until="domcontentloaded")
    page.fill('input[name="username"]', credentials["username"])
    page.fill('input[name="password"]', credentials["password"])
    page.click('button[type="submit"]')
    page.wait_for_url(re.compile(r"/admin/"), timeout=20000)

    # 2) 打开"在「使用指南」下新建内容页"的表单
    page.goto(f"{base_url}/admin/pages/add/home/standardpage/{guide_id}/", wait_until="domcontentloaded")
    expect(page.locator("#id_title")).to_be_visible(timeout=15000)

    # 3) 填写并发布
    #    标题填完必须 blur：Wagtail 的 slug 由前端在 blur 时生成，
    #    不 blur 直接提交会得到"slug 不能为空"的校验错误（真实项目里最常见的自动化假失败）
    page.fill("#id_title", PAGE_TITLE)
    page.locator("#id_title").blur()
    expect(page.locator("#id_slug")).not_to_have_value("", timeout=10000)
    page.fill("#id_intro", "由 labs/07 自动化用例创建，用于验证发布链路")

    # Wagtail 6 把发布放在 "More actions" 下拉里；必须等下拉展开、按钮可见后再点，
    # 否则点击会落在动画过程中而丢失（这是真实项目里最常见的 UI 自动化假失败）
    page.get_by_role("button", name="More actions").click()
    publish = page.get_by_role("button", name="Publish", exact=True)
    expect(publish).to_be_visible(timeout=10000)
    publish.click()

    # 4) 成功判据用"页面上的成功提示"，不用 URL：
    #    Wagtail 发布后可能跳到父页面、编辑页或列表页，URL 不是稳定契约；
    #    "created and published" 这条消息才是用户能看到的成功信号。
    expect(page.locator(".messages")).to_contain_text("published", timeout=30000)

    # 5) 前台必须能看到刚发布的页面
    resp = http.get(EXPECTED_PATH)
    assert resp.status_code == 200, f"发布后前台 {EXPECTED_PATH} 返回 {resp.status_code}"
    assert "qa ui acceptance page" in resp.text
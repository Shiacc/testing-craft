"""L3 HTTP 测试：对外契约与页面可用性。

针对真实项目，接口层要抓的是"用户可见的承诺"：
状态码、标题、关键内容存在、导航可走、静态资源可加载。
不断言整页 HTML 快照——CMS 页面每天都在改文案。
"""

from __future__ import annotations

import os
import re

import pytest

pytestmark = [pytest.mark.l3]


@pytest.mark.smoke
def test_home_returns_200_with_title(http):
    resp = http.get("/")

    assert resp.status_code == 200
    assert "<title>" in resp.text
    assert "Wagtail" in resp.text or "Welcome" in resp.text or "演示" in resp.text


@pytest.mark.parametrize(
    ("path", "expected_title"),
    [("/about/", "关于我们"), ("/guide/", "使用指南"), ("/guide/first-page/", "创建第一个页面")],
)
def test_seeded_pages_render_expected_headings(http, path, expected_title):
    resp = http.get(path)

    assert resp.status_code == 200, f"{path} 返回 {resp.status_code}"
    assert f'data-testid="page-title"' in resp.text
    assert expected_title in resp.text


@pytest.mark.smoke
def test_guide_lists_all_children(http):
    """列表页必须挂出全部子页面链接：导航断链是 CMS 最常见的线上事故。"""
    resp = http.get("/guide/")

    links = re.findall(r'data-testid="child-link"[^>]*href="([^"]+)"', resp.text)
    if not links:
        links = re.findall(r'href="(/guide/[^"]+)"', resp.text)

    assert len(links) >= 3, f"子页面链接不足：{links}"


@pytest.mark.smoke
def test_sitemap_lists_seeded_pages(http):
    resp = http.get("/sitemap.xml")

    assert resp.status_code == 200
    assert "<urlset" in resp.text
    for path in ("/about/", "/guide/"):
        assert path in resp.text, f"sitemap 缺少 {path}"


def test_admin_login_page_available(http):
    resp = http.get("/admin/login/")

    assert resp.status_code == 200
    assert 'name="username"' in resp.text and 'name="password"' in resp.text


def test_static_assets_served(http):
    """CSS 拿不到时页面"能开但很难看"，属于典型的静默降级。"""
    html = http.get("/").text
    match = re.search(r'href="(/static/[^"]+\.css)"', html)
    assert match, "首页未引用任何 CSS"

    resp = http.get(match.group(1))
    assert resp.status_code == 200
    assert "text/css" in resp.headers.get("content-type", "")


@pytest.mark.canary
@pytest.mark.skipif(
    os.environ.get("QA_RUN_CANARY") != "1",
    reason="演练用负向用例，默认跳过；需要验证缺陷上报链路时设 QA_RUN_CANARY=1",
)
def test_robots_txt_should_be_served(http):
    """【演练用负向用例】当前被测项目未提供 robots.txt（真实缺口）。

    该用例**预期失败**，用于验证"失败 → 归因 → 自动提缺陷"链路。
    它带 canary 标记，不进入常规冒烟，避免污染绿灯。
    """
    resp = http.get("/robots.txt")

    assert resp.status_code == 200, "站点缺少 robots.txt：爬虫无法获取抓取规则"
    assert "User-agent" in resp.text
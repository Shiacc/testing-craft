"""项目 2 的用例：静态结算台页面的 HTTP 契约。

这一层的选择理由：静态站点没有后端，能证明的价值集中在"页面能不能开、关键元素在不在、
静态资源能不能拿到"。它不需要 L4（没有交互状态），所以只保留 L3 三条。
"""

from __future__ import annotations

import os
import re

import httpx
import pytest

pytestmark = [pytest.mark.static]

BASE_URL = os.environ.get("QA_BASE_URL", "http://127.0.0.1:8100").rstrip("/")


@pytest.fixture(scope="module")
def http():
    with httpx.Client(base_url=BASE_URL, timeout=10.0) as client:
        yield client


def test_home_returns_html_with_title(http):
    resp = http.get("/index.html")

    assert resp.status_code == 200
    assert "text/html" in resp.headers.get("content-type", "")
    match = re.search(r"<title>(.*?)</title>", resp.text, re.S)
    assert match and "结算台" in match.group(1)


def test_key_elements_present(http):
    """关键交互元素必须存在（data-testid 是前端与测试共用的契约）。"""
    html = http.get("/index.html").text

    for testid in ("qty", "level", "coupon", "submit", "payable"):
        assert f'data-testid="{testid}"' in html, f"缺少关键元素 {testid}"


def test_static_assets_served(http):
    html = http.get("/index.html").text
    match = re.search(r'src="\./([^"]+\.js)"', html)
    assert match, "页面未引用 JS"

    resp = http.get("/" + match.group(1))
    assert resp.status_code == 200
    assert "javascript" in resp.headers.get("content-type", "")
"""L1 单元测试：被测项目自己的"纯逻辑"部分。

真实项目接入时，单元测试测的是**我们自己写的胶水/工具代码**（种子数据计划、
路径拼接、数据校验）。被测框架（Wagtail/Django）自身的逻辑由上游测试保证，
重复覆盖它既慢又无价值——这是"在能证明问题的最底层断言"的具体取舍。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))

pytestmark = [pytest.mark.l1]


def _pages_spec() -> list[dict]:
    """只取种子计划常量，不 import Django（避免本地无 DB 时炸掉）。"""
    source = (APP_DIR / "home" / "management" / "commands" / "seed_demo.py").read_text(encoding="utf-8")
    namespace: dict = {}
    start = source.index("PAGES: list[dict] = [")
    end = source.index("]\n\n\nclass Command", start) + 1
    exec(compile(source[start:end], "seed_demo.py", "exec"), namespace)  # noqa: S102 - 仅解析常量
    return namespace["PAGES"]


def test_seed_plan_has_expected_top_level_pages():
    pages = _pages_spec()

    assert [page["slug"] for page in pages] == ["about", "guide"]


def test_guide_has_three_children_with_unique_slugs():
    guide = next(page for page in _pages_spec() if page["slug"] == "guide")
    slugs = [child["slug"] for child in guide["children"]]

    assert len(slugs) == 3
    assert len(set(slugs)) == 3, "子页面 slug 必须唯一，否则幂等写入会互相覆盖"


def test_every_page_has_title_and_intro_for_assertions():
    """种子内容要"可断言"：标题与摘要是测试的判定依据，不能为空。"""
    def walk(items: list[dict]) -> list[dict]:
        result = []
        for item in items:
            result.append(item)
            result.extend(walk(item.get("children", [])))
        return result

    for page in walk(_pages_spec()):
        assert page["title"].strip(), f"{page['slug']} 缺少标题"
        assert page["intro"].strip(), f"{page['slug']} 缺少摘要"
        assert page["slug"] == page["slug"].lower(), f"{page['slug']} 应全小写"
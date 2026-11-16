"""可复现的种子数据：把"演示站点"变成"有可断言内容的被测系统"。

三条设计约束：
1. **幂等**：重复执行不产生重复页面（按 slug + parent 查后建或改），CI 可反复跑；
2. **内容可断言**：每个页面有固定标题与 intro，用例可以直接断言文案而不是"非空"；
3. **与测试解耦**：种子不引用测试代码，测试也不依赖随机数据。

用法：
    python manage.py seed_demo            # 创建/修正演示内容
    python manage.py seed_demo --reset    # 先删除 qa-demo 子树再重建
"""

from __future__ import annotations

import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from wagtail.models import Page, Site

from home.models import HomePage, StandardPage

PAGES: list[dict] = [
    {
        "slug": "about",
        "title": "关于我们",
        "intro": "演示站点：用 Wagtail 搭建的内容管理平台，作为自动化测试的真实被测对象。",
        "body": "<p>本站用于演示<strong>真实项目接入自动化测试</strong>的完整流程。</p>",
        "children": [],
    },
    {
        "slug": "guide",
        "title": "使用指南",
        "intro": "从建站到发布的内容管理入门说明。",
        "body": "<p>按顺序阅读以下章节即可完成第一次发布。</p>",
        "children": [
            {
                "slug": "first-page",
                "title": "创建第一个页面",
                "intro": "在管理后台新建页面、填写内容并发布。",
                "body": "<p>进入后台 → 页面 → 新建 → 填写标题与正文 → 发布。</p>",
            },
            {
                "slug": "publishing",
                "title": "发布与撤销发布",
                "intro": "理解草稿、发布、撤销发布三种状态。",
                "body": "<p>发布后页面才会出现在前台；撤销发布后前台返回 404。</p>",
            },
            {
                "slug": "search",
                "title": "站内搜索",
                "intro": "使用搜索框按关键词查找内容。",
                "body": "<p>搜索基于 Wagtail 内置的数据库全文检索。</p>",
            },
        ],
    },
]


class Command(BaseCommand):
    help = "创建演示内容（幂等）"

    def add_arguments(self, parser) -> None:
        parser.add_argument("--reset", action="store_true", help="删除已存在的演示子树后重建")

    def handle(self, *args, **options) -> None:
        home = HomePage.objects.first()
        if home is None:
            home = Page.objects.get(depth=1)
            self.stderr.write("未找到 HomePage，使用根页面作为父级")

        if options["reset"]:
            for child in home.get_children():
                if child.slug in {item["slug"] for item in PAGES}:
                    child.delete()
                    self.stdout.write(f"已删除 {child.slug}")

        for spec in PAGES:
            page = self._upsert(home, spec)
            for child_spec in spec.get("children", []):
                self._upsert(page, child_spec)

        self._write_state(home)

        site = Site.objects.filter(is_default_site=True).first()
        if site and site.root_page_id != home.id:
            site.root_page = home
            site.save()

        self.stdout.write(self.style.SUCCESS(f"演示内容就绪：{Page.objects.count()} 个页面"))

    def _write_state(self, home: Page) -> None:
        """把页面 ID 落盘给 UI 测试用：Wagtail 后台新建页面的 URL 需要父页面 ID。

        这是测试夹具数据而不是后门：只写 ID 与 slug，不含任何凭据。
        """
        state = {
            "home_id": home.id,
            "pages": {p.slug: p.id for p in home.get_children()},
            "children": {
                parent.slug: {child.slug: child.id for child in parent.get_children()}
                for parent in home.get_children()
            },
        }
        path = Path(settings.BASE_DIR) / ".seed_state.json"
        path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        self.stdout.write(f"已写入 {path.name}")

    def _upsert(self, parent: Page, spec: dict) -> Page:
        existing = parent.get_children().filter(slug=spec["slug"]).first()
        if existing is not None:
            # get_children() 返回的可能是不含子类字段的基类实例，必须走 .specific
            page = existing.specific
            page.title = spec["title"]
            if hasattr(page, "intro"):
                page.intro = spec["intro"]
                page.body = spec["body"]
            page.save_revision().publish()
            self.stdout.write(f"已更新 {spec['slug']}")
            return page

        page = parent.add_child(
            instance=StandardPage(
                title=spec["title"],
                slug=spec["slug"],
                intro=spec.get("intro", ""),
                body=spec.get("body", ""),
            )
        ).specific
        page.save_revision().publish()
        self.stdout.write(f"已创建 {spec['slug']}")
        return page
"""清理 UI 测试留下的页面（slug 以 qa- 开头）。

为什么要单独一个命令而不是在测试里直接改数据库：
- 测试只走 HTTP/UI，不越过被测系统边界（越界清理会掩盖真实问题）；
- 清理是"测试数据治理"的一部分，必须可单独执行、可被 CI 复用、可在失败后补跑。

用法：
    python manage.py cleanup_qa_pages            # 删除所有 qa- 前缀页面
    python manage.py cleanup_qa_pages --dry-run  # 只列出不删除
"""

from __future__ import annotations

from django.core.management.base import BaseCommand
from wagtail.models import Page

PREFIX = "qa-"


class Command(BaseCommand):
    help = "删除 slug 以 qa- 开头的测试页面"

    def add_arguments(self, parser) -> None:
        parser.add_argument("--dry-run", action="store_true", help="只列出，不删除")

    def handle(self, *args, **options) -> None:
        targets = [page for page in Page.objects.all() if page.slug.startswith(PREFIX)]
        if not targets:
            self.stdout.write("没有需要清理的测试页面")
            return

        for page in targets:
            if options["dry_run"]:
                self.stdout.write(f"[dry-run] 将删除 {page.slug}（id={page.id}）")
                continue
            page.delete()
            self.stdout.write(f"已删除 {page.slug}（id={page.id}）")

        self.stdout.write(self.style.SUCCESS(f"清理完成：{len(targets)} 个页面"))
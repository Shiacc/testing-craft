"""创建/更新本地测试账号（幂等）。

为什么做成管理命令而不是在脚本里内联 python：
- 内联脚本在不同 shell / 编码下极易碎（引号、中文、BOM），管理命令只依赖 Django；
- 账号口径集中在一处：用户名、密码策略、权限边界都可审。
"""

from __future__ import annotations

import os

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

USERNAME = os.environ.get("QA_USERNAME", "qa")
PASSWORD = os.environ.get("QA_PASSWORD", "qa-pass-12345")


class Command(BaseCommand):
    help = "创建或更新本地自动化测试账号（仅用于本地 sqlite 环境）"

    def add_arguments(self, parser) -> None:
        parser.add_argument("--username", default=USERNAME)
        parser.add_argument("--password", default=PASSWORD)

    def handle(self, *args, **options) -> None:
        model = get_user_model()
        username = options["username"]
        user, created = model.objects.get_or_create(
            username=username, defaults={"email": f"{username}@example.com"}
        )
        user.is_staff = True
        user.is_superuser = True
        user.set_password(options["password"])
        user.save()

        action = "创建" if created else "更新"
        self.stdout.write(self.style.SUCCESS(f"测试账号 {username} 已{action}（仅本地环境）"))
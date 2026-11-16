"""被测应用（Wagtail 全栈 CMS）的页面模型。

实验定位：这是一个**真实的开源全栈项目**（Wagtail 6.4 / Django 5.2），
我们只做最小改动以便被自动化测试驱动：
1. 增加一个通用内容页 `StandardPage`，用于覆盖「创建 → 发布 → 前台可见」这条主流程；
2. 其余保持脚手架默认，避免为了测试改写被测系统（那等于给自己作弊）。

真实项目接入时也应遵守同样的克制：只加"便于隔离与观测"的最小改动，
不改业务语义、不加测试后门。
"""

from django.db import models

from wagtail.admin.panels import FieldPanel
from wagtail.fields import RichTextField
from wagtail.models import Page
from wagtail.search import index


class HomePage(Page):
    """项目脚手架自带首页，保持原样。"""


class StandardPage(Page):
    """通用内容页：标题 + 摘要 + 富文本正文。"""

    intro = models.CharField(max_length=250, blank=True, help_text="用于列表与搜索摘要")
    body = RichTextField(blank=True)

    content_panels = Page.content_panels + [
        FieldPanel("intro"),
        FieldPanel("body"),
    ]

    search_fields = Page.search_fields + [
        index.SearchField("intro"),
        index.SearchField("body"),
    ]
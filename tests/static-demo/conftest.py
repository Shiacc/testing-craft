"""项目 2 的测试夹具：声明 marker、锁定被测地址来源。

约定与项目 1 一致：地址由平台注入 `QA_BASE_URL`，不写死在测试里，
这样同一份用例可以在本地、CI、任意端口复用。
"""

from __future__ import annotations


def pytest_configure(config) -> None:
    config.addinivalue_line("markers", "static: 静态站点 HTTP 契约")
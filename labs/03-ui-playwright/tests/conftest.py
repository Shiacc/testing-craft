"""Playwright 实验的公共设施：本地静态服务器、浏览器、失败自动留证。

三个 fixture 各解决一个工程问题：
- `static_server`：被测站点随测试一起启动，端口随机，避免端口冲突（环境治理）；
- `browser`：整个会话只启动一次浏览器，用独立 context 隔离用例（性能 + 隔离）；
- `page`：失败时自动保存截图与 trace，成功时只保留 trace 元数据（失败可观、不产垃圾）。
"""

from __future__ import annotations

import os
import socket
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from playwright.sync_api import Browser, Page, sync_playwright

WEB_DIR = Path(__file__).resolve().parents[1] / "web"
ARTIFACT_DIR = Path(__file__).resolve().parents[1] / "artifacts"


class _QuietHandler(SimpleHTTPRequestHandler):
    """默认 handler 会把每个请求打到 stderr，CI 日志会被噪声淹没。"""

    def log_message(self, *args, **kwargs) -> None:  # noqa: D401
        return


@pytest.fixture(scope="session")
def static_server() -> str:
    handler = partial(_QuietHandler, directory=str(WEB_DIR))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    base_url = f"http://127.0.0.1:{port}"
    # 就绪探测：避免测试在服务器还没 listen 完成时发出第一个请求（典型时序 flaky）
    for _ in range(50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                break
        threading.Event().wait(0.05)

    yield base_url
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)


@pytest.fixture(scope="session")
def browser() -> Browser:
    """优先用 Playwright 自带 chromium；未下载时回落到系统 Chrome / Edge。

    这不是"偷懒"，而是企业环境的常态：CI 沙箱常常拉不到浏览器二进制，
    硬编码 `chromium.launch()` 会让整条流水线直接红。用显式回落链 + 环境变量
    覆盖（PW_CHANNEL=msedge）比"在 README 里写一句请先手工安装"可靠得多。
    """
    headed = os.environ.get("HEADED") == "1"
    forced = os.environ.get("PW_CHANNEL")
    candidates: list[str | None] = ([forced] if forced else []) + [None, "chrome", "msedge"]

    with sync_playwright() as pw:
        errors: list[str] = []
        for channel in candidates:
            kwargs: dict = {"headless": not headed}
            if channel:
                kwargs["channel"] = channel
            try:
                browser = pw.chromium.launch(**kwargs)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{channel or 'bundled-chromium'}: {exc}".splitlines()[0])
                continue
            print(f"\n[lab03] browser = {channel or 'bundled-chromium'}")
            yield browser
            browser.close()
            return
        raise RuntimeError("没有可用的浏览器：\n  " + "\n  ".join(errors))


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """把"这一步是否失败"回写到用例对象，供 page fixture 决定是否留证。"""
    outcome = yield
    report = outcome.get_result()
    if report.when == "call" and report.failed:
        setattr(item, "_labs_failed", True)


@pytest.fixture()
def page(browser: Browser, request) -> Page:
    context = browser.new_context(viewport={"width": 1280, "height": 900}, locale="zh-CN")
    context.tracing.start(screenshots=True, snapshots=True, sources=False)
    page = context.new_page()

    yield page

    failed = bool(getattr(request.node, "_labs_failed", False))
    if failed:
        ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
        safe_name = request.node.name.replace("[", "_").replace("]", "").replace("/", "_")
        try:
            page.screenshot(path=str(ARTIFACT_DIR / f"{safe_name}.png"), full_page=True)
            context.tracing.stop(path=str(ARTIFACT_DIR / f"{safe_name}-trace.zip"))
        except Exception:  # noqa: BLE001 - 留证失败不应掩盖原始失败
            context.tracing.stop()
    else:
        context.tracing.stop()

    context.close()
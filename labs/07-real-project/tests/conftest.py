"""真实项目接入的公共设施。

被测系统：Wagtail 6.4 / Django 5.2 全栈 CMS（labs/07-real-project/app）
访问地址：默认 http://127.0.0.1:8099，可用 QA_BASE_URL 覆盖
账号：qa / qa-pass-12345（本地开发库，由 bootstrap 创建）

三条边界：
1. 测试只通过 HTTP/UI 访问被测系统，不 import 它的模型（除 L1 纯逻辑用例）；
2. 需要"夹具数据"（如父页面 ID）时读 seed 落盘的 .seed_state.json，不写死数字；
3. UI 产生的测试页面统一用 qa- 前缀，teardown 调用管理命令清理。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

LAB_DIR = Path(__file__).resolve().parents[1]
APP_DIR = LAB_DIR / "app"
BASE_URL = os.environ.get("QA_BASE_URL", "http://127.0.0.1:8099").rstrip("/")
USERNAME = os.environ.get("QA_USERNAME", "qa")
PASSWORD = os.environ.get("QA_PASSWORD", "qa-pass-12345")
STATE_FILE = APP_DIR / ".seed_state.json"


def pytest_configure(config: pytest.Config) -> None:
    for marker, desc in (
        ("smoke", "冒烟：核心可用性"),
        ("l1", "L1 单元测试（纯逻辑）"),
        ("l3", "L3 HTTP/接口测试"),
        ("l4", "L4 UI 端到端测试"),
        ("canary", "演练用负向用例：预期失败，用于验证缺陷上报链路"),
    ):
        config.addinivalue_line("markers", f"{marker}: {desc}")


@pytest.fixture(scope="session")
def base_url() -> str:
    try:
        httpx.get(BASE_URL + "/", timeout=5.0)
    except Exception as exc:  # noqa: BLE001
        pytest.exit(
            f"被测站点不可达 {BASE_URL}：{exc}\n先执行：cd labs/07-real-project && ./bootstrap.ps1",
            returncode=3,
        )
    return BASE_URL


@pytest.fixture(scope="session")
def http(base_url: str):
    with httpx.Client(base_url=base_url, timeout=15.0, follow_redirects=False) as client:
        yield client


@pytest.fixture(scope="session")
def seed_state() -> dict:
    if not STATE_FILE.exists():
        pytest.skip(f"缺少夹具数据 {STATE_FILE.name}，先运行：python manage.py seed_demo")
    return json.loads(STATE_FILE.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def credentials() -> dict[str, str]:
    return {"username": USERNAME, "password": PASSWORD}


@pytest.fixture(scope="session")
def browser():
    """优先 Playwright 自带 chromium，缺失时回落系统 Chrome / Edge。"""
    from playwright.sync_api import sync_playwright

    forced = os.environ.get("PW_CHANNEL")
    candidates: list[str | None] = ([forced] if forced else []) + [None, "chrome", "msedge"]
    errors: list[str] = []
    with sync_playwright() as pw:
        for channel in candidates:
            kwargs: dict = {"headless": os.environ.get("HEADED") != "1"}
            if channel:
                kwargs["channel"] = channel
            try:
                instance = pw.chromium.launch(**kwargs)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{channel or 'bundled'}: {exc}".splitlines()[0])
                continue
            yield instance
            instance.close()
            return
    pytest.skip("没有可用浏览器：\n  " + "\n  ".join(errors))


@pytest.fixture()
def page(browser):
    context = browser.new_context(viewport={"width": 1440, "height": 900}, locale="zh-CN")
    page = context.new_page()
    yield page
    context.close()


@pytest.fixture(scope="session", autouse=True)
def _cleanup_before_and_after():
    """跑前清一次（清除上次失败残留），跑后再清一次（不留垃圾）。"""
    _run_cleanup()
    yield
    _run_cleanup()


def _run_cleanup() -> None:
    if not APP_DIR.exists():
        return
    subprocess.run(
        [sys.executable, "manage.py", "cleanup_qa_pages"],
        cwd=APP_DIR,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
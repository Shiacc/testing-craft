"""本地自建禅道（Docker），用于验证缺陷适配器 —— 不需要任何外部账号。

做的事：
1. 起容器（内置 MariaDB，`MYSQL_INTERNAL=true`），等 HTTP 就绪；
2. 用 Playwright 走完安装向导（许可 → 环境 → 数据库 → 建表 → 选模式 → 建管理员）；
3. 通过 REST API 取 token、确保存在一个产品；
4. 打印可直接 `$env:...` 使用的变量。

用法：
    python scripts/zentao_local.py                     # 起容器 + 安装 + 打印变量
    python scripts/zentao_local.py --verify-only       # 只验证已装好的实例并打印变量
    python scripts/zentao_local.py --port 8300 --reset # 先删容器再重来

为什么要有这个脚本：缺陷适配器的真实链路上，"没有测试环境"是最大阻塞。
用 Docker 自建一个禅道，适配器就能被真实验证，而不是只对着 mock 自说自话。
（Jira 同理需要授权码/账号，官方镜像装不上免费许可，故 Jira 侧仍以 mock 验证。）
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
CONTAINER = "zentao-qa"
IMAGE = "easysoft/zentao:latest"
ADMIN_ACCOUNT = "admin"
ADMIN_PASSWORD = "Zentao@2026"
ADMIN_COMPANY = "QA"
PRODUCT_NAME = "QA 演练产品"


def sh(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")


def docker_up(port: int, reset: bool) -> None:
    if reset:
        sh(["docker", "rm", "-f", CONTAINER])
    existing = sh(["docker", "ps", "-a", "--filter", f"name={CONTAINER}", "--format", "{{.Names}}"]).stdout.strip()
    if existing:
        sh(["docker", "start", CONTAINER])
        print(f"[docker] 复用已有容器 {CONTAINER}")
    else:
        out = sh(
            [
                "docker",
                "run",
                "-d",
                "--name",
                CONTAINER,
                "-p",
                f"{port}:80",
                "-e",
                "MYSQL_INTERNAL=true",
                "-e",
                "MYSQL_PASSWORD=123456",
                IMAGE,
            ]
        )
        if out.returncode != 0:
            raise SystemExit(f"[docker] 启动失败：{out.stderr[-300:]}")
        print(f"[docker] 已启动 {CONTAINER}，端口 {port}")


def wait_http(base: str, timeout_s: int = 240) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            resp = httpx.get(base + "/install.php", timeout=5, follow_redirects=False)
            if resp.status_code < 500:
                print(f"[http] 就绪（{resp.status_code}）")
                return
        except Exception:  # noqa: BLE001
            pass
        time.sleep(3)
    raise SystemExit("[http] 超时：服务未就绪")


def installed(base: str) -> bool:
    """已安装的判定：/index.php 不再把浏览器打回 install.php。

    注意不能用 status==200 判断：安装完成后首次访问常会 302 到 upgrade/safeDelete，
    那也是"已安装"。
    """
    try:
        resp = httpx.get(base + "/index.php", timeout=10, follow_redirects=False)
        location = resp.headers.get("location", "")
        return resp.status_code == 200 or "install.php" not in location
    except Exception:  # noqa: BLE001
        return False


def install(base: str) -> None:
    """走完安装向导。所有步骤在同一个 page 会话里完成（向导依赖 session）。"""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 1000}, locale="zh-CN")
        page.on("dialog", lambda d: d.accept())

        page.goto(base + "/install.php", wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(800)
        page.click("text=开始安装")
        page.wait_for_timeout(1200)

        for tag in ("license", "step1", "step2"):
            if tag == "step2":
                box = page.locator("input[name=clearDB]")
                if box.count():
                    box.first.check()
            page.locator("text=下一步").first.click(force=True)
            page.wait_for_timeout(1600)
            print(f"[install] {tag} 完成")

        print("[install] 建表中（最多 8 分钟）…")
        deadline = time.time() + 480
        while time.time() < deadline:
            if "正在安装" not in page.inner_text("body"):
                break
            time.sleep(5)
        print("[install] 建表结束")

        # 配置页必须提交一次：它会写入 installed 标记；跳过这步，后面的管理员保存无效
        if page.locator("text=下一步").count():
            page.locator("text=下一步").first.click(force=True)
            page.wait_for_timeout(2200)
            print("[install] 配置已保存")

        # 选模式（step4 可独立访问）
        page.goto(base + "/install.php?m=install&f=step4", wait_until="domcontentloaded")
        page.wait_for_timeout(1800)
        for label in ("使用轻量管理模式", "轻量"):
            if page.locator(f"text={label}").count():
                page.locator(f"text={label}").first.click(force=True)
                page.wait_for_timeout(2200)
                break

        # 管理员表单
        fields = page.eval_on_selector_all("input[name]", "els => els.map(e => e.name)")
        if "account" not in fields:
            print(f"[install] 警告：当前页面不是管理员表单（fields={fields[:6]}）")
        for name in fields:
            low = name.lower()
            if "company" in low:
                page.fill(f"input[name={name}]", ADMIN_COMPANY)
            elif "password" in low or "passwd" in low:
                page.fill(f"input[name={name}]", ADMIN_PASSWORD)
            elif low.endswith("account") or low in ("account", "username"):
                page.fill(f"input[name={name}]", ADMIN_ACCOUNT)
        for label in ("保存", "下一步", "提交"):
            if page.locator(f"text={label}").count():
                page.locator(f"text={label}").first.click(force=True)
                break
        page.wait_for_timeout(3000)
        browser.close()


def api_token(base: str) -> str:
    resp = httpx.post(
        base + "/api.php/v1/tokens",
        json={"account": ADMIN_ACCOUNT, "password": ADMIN_PASSWORD},
        timeout=20,
    )
    if resp.status_code >= 300:
        raise SystemExit(f"[api] 取 token 失败：HTTP {resp.status_code} {resp.text[:200]}")
    return resp.json()["token"]


def ensure_product(base: str, token: str) -> int:
    headers = {"Token": token}
    resp = httpx.get(base + "/api.php/v1/products", headers=headers, timeout=20)
    products = (resp.json() or {}).get("products") or []
    if products:
        return int(products[0]["id"])
    created = httpx.post(
        base + "/api.php/v1/products",
        headers=headers,
        json={"name": PRODUCT_NAME, "code": "qa-demo"},
        timeout=20,
    )
    if created.status_code >= 300:
        raise SystemExit(f"[api] 创建产品失败：HTTP {created.status_code} {created.text[:200]}")
    return int(created.json()["id"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="本地 Docker 自建禅道，用于验证缺陷适配器")
    parser.add_argument("--port", type=int, default=8300)
    parser.add_argument("--verify-only", action="store_true", help="跳过启动与安装，只验证并打印变量")
    parser.add_argument("--reset", action="store_true", help="先删除容器再重来")
    args = parser.parse_args(argv)

    base = f"http://127.0.0.1:{args.port}"

    if not args.verify_only:
        docker_up(args.port, args.reset)
        wait_http(base)
        if installed(base):
            print("[install] 检测到已安装，跳过向导")
        else:
            install(base)
            if not installed(base):
                raise SystemExit("[install] 安装后仍不可用，请查看容器日志：docker logs " + CONTAINER)
            print("[install] 安装完成")

    token = api_token(base)
    product_id = ensure_product(base, token)
    print("\n=== 可用变量（PowerShell）===")
    print(f'$env:ZENTAO_BASE_URL="{base}"')
    print(f'$env:ZENTAO_PRODUCT_ID="{product_id}"')
    print(f'$env:ZENTAO_ACCOUNT="{ADMIN_ACCOUNT}"')
    print(f'$env:ZENTAO_PASSWORD="{ADMIN_PASSWORD}"')
    print("\n下一步：python -m atf tracker check --tracker zentao")
    return 0


if __name__ == "__main__":
    sys.exit(main())
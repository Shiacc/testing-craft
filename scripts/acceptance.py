"""本地完整验收：起禅道 → 体检 → 跑全流程 → 真实提单 → 出结论。

为什么需要它：单点脚本已经齐了（zentao_local.py / atf e2e / tracker check），
但"验收"是一个有顺序、有前置、要判定成败的流程，必须固化成一条命令，
否则每次都要人记顺序、对日志。

用法：
    python scripts/acceptance.py                 # 默认：复用已安装的禅道，跑完整验收并真实提单
    python scripts/acceptance.py --fresh         # 先重建容器（全新安装，约 5 分钟）
    python scripts/acceptance.py --no-submit     # 只跑到 mock 提单（不碰真实系统）
    python scripts/acceptance.py --port 8300

退出码：0 全部通过 / 1 有用例失败或提单失败 / 3 环境未就绪
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

ADMIN_ACCOUNT = "admin"
ADMIN_PASSWORD = "Zentao@2026"


def step(msg: str) -> None:
    print(f"\n=== {msg} ===", flush=True)


def run(cmd: list[str], env: dict | None = None) -> subprocess.CompletedProcess:
    merged = dict(os.environ, PYTHONUTF8="1", **(env or {}))
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", env=merged)
    return proc


def ensure_zentao(port: int, fresh: bool) -> tuple[str, int]:
    step("1/5 准备本地禅道（Docker）")
    cmd = [sys.executable, "-X", "utf8", "scripts/zentao_local.py", "--port", str(port)]
    if fresh:
        cmd.append("--reset")
    else:
        cmd.append("--verify-only")
    proc = run(cmd)
    if proc.returncode != 0:
        print(proc.stdout[-1500:])
        print(proc.stderr[-800:])
        raise SystemExit("[FAIL] 禅道未就绪")

    text = proc.stdout
    base = re.search(r'ZENTAO_BASE_URL="([^"]+)"', text)
    product = re.search(r'ZENTAO_PRODUCT_ID="([^"]+)"', text)
    if not base or not product:
        raise SystemExit("[FAIL] 未能从输出解析出禅道地址/产品 ID")
    print(f"  禅道：{base.group(1)}  产品：{product.group(1)}")
    return base.group(1), int(product.group(1))


def check_tracker(base: str, product_id: int) -> None:
    step("2/5 缺陷系统体检")
    env = {
        "ZENTAO_BASE_URL": base,
        "ZENTAO_PRODUCT_ID": str(product_id),
        "ZENTAO_ACCOUNT": ADMIN_ACCOUNT,
        "ZENTAO_PASSWORD": ADMIN_PASSWORD,
    }
    proc = run([sys.executable, "-m", "atf", "tracker", "check", "--tracker", "zentao"], env)
    for line in (proc.stdout or "").strip().splitlines()[-4:]:
        print("  " + line)
    if proc.returncode != 0:
        raise SystemExit("[FAIL] 体检未通过")


def run_e2e(base: str, product_id: int, submit: bool) -> tuple[int, str]:
    step("3/5 端到端（重置 → 起服务 → 分层执行 → 归因）" + ("→ 真实提单" if submit else ""))
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    state_dir = ROOT / "atf" / "state" / f"acceptance-{stamp}"
    state_dir.mkdir(parents=True, exist_ok=True)

    env = {
        "ZENTAO_BASE_URL": base,
        "ZENTAO_PRODUCT_ID": str(product_id),
        "ZENTAO_ACCOUNT": ADMIN_ACCOUNT,
        "ZENTAO_PASSWORD": ADMIN_PASSWORD,
        # 每次验收独立去重表：保证"每次验收都真的提一单"，而不是被上次的记录判成 duplicate
        "ATF_STATE_DIR": str(state_dir),
    }
    mode = "submit" if submit else "mock"
    cmd = [
        sys.executable, "-m", "atf", "e2e",
        "--project", "wagtail-demo",
        "--filing", mode,
        "--tracker", "zentao",
    ]
    proc = run(cmd, env)
    out = (proc.stdout or "") + (proc.stderr or "")
    for line in out.strip().splitlines()[-10:]:
        print("  " + line)

    filed = re.search(r"\[filed\]\s+(\S+)\s+zentao\s+(\d+)", out)
    bug_id = filed.group(2) if filed else ""
    if proc.returncode != 0:
        return proc.returncode, bug_id
    return 0, bug_id


def verify_bug(base: str, bug_id: str) -> None:
    step("4/5 复核缺陷单（禅道 API）")
    with httpx.Client(base_url=base, timeout=20) as client:
        token = client.post(
            "/api.php/v1/tokens", json={"account": ADMIN_ACCOUNT, "password": ADMIN_PASSWORD}
        ).json()["token"]
        headers = {"Token": token}
        if bug_id:
            resp = client.get(f"/api.php/v1/bugs/{bug_id}", headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                print(f"  #{data.get('id')} {data.get('title')}")
                print(f"  severity={data.get('severity')} pri={data.get('pri')} status={data.get('status')}")
        listing = client.get("/api.php/v1/products/1/bugs", headers=headers).json()
        print(f"  产品缺陷总数：{listing.get('total')}")


def summary(start: float, submit: bool, bug_id: str) -> None:
    step("5/5 结论")
    print(f"  耗时：{time.time() - start:.1f}s")
    print(f"  缺陷联动：{'真实禅道' if submit else 'mock'}  单号：{bug_id or '-'}")
    print(f"  产物：atf/e2e-runs/wagtail-demo/（RUN.md / results.json / junit-*.xml）")
    print("  提示：容器可用 docker rm -f zentao-qa 停止；缺陷单在 http://127.0.0.1:8300 里查看")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="本地完整验收（含真实禅道提单）")
    parser.add_argument("--port", type=int, default=8300)
    parser.add_argument("--fresh", action="store_true", help="重建容器（全新安装）")
    parser.add_argument("--no-submit", action="store_true", help="不真实提单，只跑 mock")
    args = parser.parse_args(argv)

    started = time.time()
    print(f"本地完整验收 · {datetime.now():%Y-%m-%d %H:%M:%S}")
    base, product_id = ensure_zentao(args.port, args.fresh)
    check_tracker(base, product_id)
    code, bug_id = run_e2e(base, product_id, submit=not args.no_submit)
    if bug_id:
        verify_bug(base, bug_id)
    summary(started, not args.no_submit, bug_id)
    return code


if __name__ == "__main__":
    sys.exit(main())
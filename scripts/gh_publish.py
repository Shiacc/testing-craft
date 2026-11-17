"""把仓库发布到 GitHub 并跑一次 Actions（免费公开仓库，Actions 分钟数不限量）。

为什么单独写脚本而不是让用户手点：
- 建仓、加远端、推送、触发、轮询、判定，是一串需要"顺序 + 断言"的动作；
- 每一步都要给出证据（HTTP 状态、run 结论、URL），而不是"应该成功了吧"。

安全约定：
- token 只从**文件或环境变量**读，不写进仓库、不写进 git remote URL；
- 打印时一律脱敏（只显示前 4 位与长度）；
- 推送用 `http.extraheader` 传 token，命令结束后 remote 里不留凭据。

用法：
    python scripts/gh_publish.py --token-file ..\\.tmp\\gh-token.txt            # 建仓+推送+触发+等待
    python scripts/gh_publish.py --token-file <file> --dry-run                  # 只打印计划，不调 API
    python scripts/gh_publish.py --token-file <file> --repo my-tc --private

退出码：0 推送成功且（若 --wait）Actions 通过 / 1 Actions 失败 / 2 参数或 token 问题 / 3 推送失败
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
API = "https://api.github.com"
WORKFLOW_FILE = ".github/workflows/qa-e2e.yml"


def mask(token: str) -> str:
    return f"{token[:4]}***（len={len(token)}）" if token else "(空)"


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def api(method: str, path: str, token: str, **kwargs):
    import httpx

    with httpx.Client(base_url=API, timeout=30.0, headers=_headers(token)) as client:
        return client.request(method, path, **kwargs)


def read_token(path: str | None) -> str:
    if path:
        candidate = Path(path)
        if not candidate.exists():
            raise SystemExit(f"[错误] token 文件不存在：{candidate}")
        return candidate.read_text(encoding="utf-8-sig").strip()
    return os.environ.get("GITHUB_TOKEN", "") or os.environ.get("GH_TOKEN", "")


def ensure_repo(token: str, owner: str, repo: str, private: bool, dry_run: bool) -> dict:
    if dry_run:
        body = {"name": repo, "private": private, "description": "QA 全流程（dry-run 计划）"}
        print(f"[dry-run] 将创建{{'私有' if private else '公开'}}仓库 {owner}/{repo}：{json.dumps(body, ensure_ascii=False)}")
        return {"full_name": f"{owner}/{repo}", "private": private}

    resp = api("GET", f"/repos/{owner}/{repo}", token)
    if resp.status_code == 200:
        print(f"[仓库] 已存在：{resp.json()['full_name']}（private={resp.json()['private']}）")
        return resp.json()
    if resp.status_code != 404:
        print(f"[仓库] 查询失败 HTTP {resp.status_code}：{resp.text[:200]}")
        raise SystemExit(2)

    body = {
        "name": repo,
        "description": "QA 全流程：真实项目接入 + 用例库 + 缺陷联动 + 一条命令端到端",
        "private": private,
        "has_issues": True,
        "has_wiki": False,
    }
    if dry_run:
        print(f"[dry-run] 将创建公开仓库 {owner}/{repo}：{json.dumps(body, ensure_ascii=False)}")
        return {"full_name": f"{owner}/{repo}", "private": private}
    created = api("POST", "/user/repos", token, json=body)
    if created.status_code >= 300:
        print(f"[仓库] 创建失败 HTTP {created.status_code}：{created.text[:300]}")
        print("        常见原因：token 缺少 Administration:write 或 repo 权限")
        raise SystemExit(2)
    print(f"[仓库] 已创建：{created.json()['full_name']}（{created.json()['html_url']}）")
    return created.json()


def push(token: str, owner: str, repo: str, branch: str, dry_run: bool) -> None:
    auth = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    url = f"https://github.com/{owner}/{repo}.git"
    cmd = [
        "git",
        "-c",
        f"http.https://github.com/.extraheader=AUTHORIZATION: basic {auth}",
        "push",
        "--set-upstream",
        url,
        f"{branch}:{branch}",
    ]
    if dry_run:
        print(f"[dry-run] 将推送 {branch} → {url}（token 经 extraheader 传递，不写入 remote）")
        return
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
    tail = ((proc.stderr or "") + (proc.stdout or "")).strip().splitlines()[-3:]
    print("[推送] " + (" | ".join(tail) if tail else f"exit {proc.returncode}"))
    if proc.returncode != 0:
        raise SystemExit(3)
    # 记录一个不含凭据的远端，方便以后手工操作
    subprocess.run(["git", "remote", "remove", "github"], cwd=ROOT, capture_output=True)
    subprocess.run(["git", "remote", "add", "github", url], cwd=ROOT, capture_output=True)


def latest_run(token: str, owner: str, repo: str) -> dict | None:
    resp = api("GET", f"/repos/{owner}/{repo}/actions/runs", token, params={"per_page": 5})
    if resp.status_code != 200:
        return None
    runs = resp.json().get("workflow_runs") or []
    return runs[0] if runs else None


def wait_run(token: str, owner: str, repo: str, timeout_s: int = 900) -> dict | None:
    deadline = time.time() + timeout_s
    seen: dict | None = None
    while time.time() < deadline:
        run = latest_run(token, owner, repo)
        if run:
            seen = run
            status, conclusion = run.get("status"), run.get("conclusion")
            print(f"[Actions] {run.get('name')} #{run.get('run_number')} status={status} conclusion={conclusion or '-'}")
            if status == "completed":
                return run
        time.sleep(15)
    return seen


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="发布到 GitHub 并跑一次 Actions")
    parser.add_argument("--token-file", default=None, help="保存 PAT 的本地文件（推荐；不要把 token 写进仓库）")
    parser.add_argument("--repo", default="testing-craft", help="仓库名，默认 testing-craft")
    parser.add_argument("--branch", default="master")
    parser.add_argument("--private", action="store_true", help="默认公开（Actions 免费不限量）")
    parser.add_argument("--wait", action="store_true", help="等待 Actions 跑完并据结论返回")
    parser.add_argument("--timeout", type=int, default=900, help="等待 Actions 的超时（秒）")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划，不调 API、不推送")
    args = parser.parse_args(argv)

    token = read_token(args.token_file)
    if not token:
        print("[错误] 没有读到 token：用 --token-file 或设置 GITHUB_TOKEN")
        return 2
    print(f"[token] {mask(token)}")

    if args.dry_run:
        print("[dry-run] 跳过身份校验，直接演示流程")
        owner = "<你的 GitHub 用户名>"
        ensure_repo(token, owner, args.repo, args.private, dry_run=True)
        push(token, owner, args.repo, args.branch, dry_run=True)
        print("[dry-run] 将等待 workflow：qa-e2e（push 触发）")
        return 0

    me = api("GET", "/user", token)
    if me.status_code != 200:
        print(f"[错误] token 无效或权限不足：HTTP {me.status_code} {me.text[:150]}")
        return 2
    owner = me.json()["login"]
    print(f"[身份] {owner}")

    ensure_repo(token, owner, args.repo, args.private, dry_run=False)
    push(token, owner, args.repo, args.branch, dry_run=False)

    if not args.wait:
        print(f"[完成] 打开 https://github.com/{owner}/{args.repo}/actions 查看运行结果")
        return 0

    print("[Actions] 等待最近一次运行（首次可能排队 10-30 秒）…")
    time.sleep(10)
    run = wait_run(token, owner, args.repo, timeout_s=args.timeout)
    if run is None:
        print("[Actions] 没有找到运行记录：确认仓库已启用 Actions、workflow 文件在 .github/workflows/ 下")
        return 1
    conclusion = run.get("conclusion")
    print(f"[Actions] 结论：{conclusion}｜{run.get('html_url')}")
    return 0 if conclusion == "success" else 1


if __name__ == "__main__":
    sys.exit(main())
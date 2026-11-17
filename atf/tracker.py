"""缺陷系统体检：真实接入前先"GET 一次元数据"，别等提交时才撞字段错误。

为什么单独做这一步：
- 禅道/Jira 的字段名、必填项、枚举值随版本与实例配置变化，**不能凭文档假设**；
- 真实事故里最常见的是"提单 400 才发现项目 key 拼错/字段不存在"；
- 所以提交链路必须前置一次只读体检：项目存在吗？产品存在吗？缺陷类型可用吗？

两种用法：
    python -m atf tracker check --tracker both            # 用环境变量里的真实凭据体检
    python -m atf tracker check --tracker both --mock     # 起本地 mock，验证体检逻辑本身可用

退出码：0 全部通过 / 3 缺凭据或体检不通过（与 e2e 的环境类错误保持一致）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import httpx


@dataclass
class CheckResult:
    tracker: str
    name: str
    ok: bool
    detail: str = ""


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def check_zentao(timeout: float = 10.0) -> list[CheckResult]:
    base = _env("ZENTAO_BASE_URL").rstrip("/")
    product_id = _env("ZENTAO_PRODUCT_ID")
    token = _env("ZENTAO_TOKEN")
    account, password = _env("ZENTAO_ACCOUNT"), _env("ZENTAO_PASSWORD")

    missing = [name for name, value in (("ZENTAO_BASE_URL", base), ("ZENTAO_PRODUCT_ID", product_id)) if not value]
    if not token and not (account and password):
        missing.append("ZENTAO_TOKEN 或 ZENTAO_ACCOUNT+ZENTAO_PASSWORD")
    if missing:
        return [CheckResult("zentao", "凭据", False, "缺少环境变量：" + "、".join(missing))]

    headers = {"Token": token} if token else {}
    results: list[CheckResult] = []
    try:
        with httpx.Client(base_url=base, timeout=timeout, headers=headers) as client:
            if not token:
                resp = client.post("/api.php/v1/tokens", json={"account": account, "password": password})
                if resp.status_code >= 400:
                    return [CheckResult("zentao", "换取 token", False, f"HTTP {resp.status_code}：{resp.text[:120]}")]
                headers = {"Token": resp.json().get("token", "")}
                client.headers.update(headers)
                results.append(CheckResult("zentao", "换取 token", True, "账号密码认证可用"))

            resp = client.get("/api.php/v1/products")
            if resp.status_code >= 400:
                results.append(CheckResult("zentao", "产品列表", False, f"HTTP {resp.status_code}：{resp.text[:120]}"))
                return results
            products = (resp.json() or {}).get("products") or []
            ids = {str(item.get("id")) for item in products}
            ok = str(product_id) in ids
            results.append(
                CheckResult(
                    "zentao",
                    "目标产品",
                    ok,
                    f"product_id={product_id} " + ("存在" if ok else f"不存在（现有：{', '.join(sorted(ids)) or '空'}）"),
                )
            )
    except Exception as exc:  # noqa: BLE001
        results.append(CheckResult("zentao", "连通性", False, f"{type(exc).__name__}: {exc}"))
    return results


def check_jira(timeout: float = 10.0) -> list[CheckResult]:
    base = _env("JIRA_BASE_URL").rstrip("/")
    email = _env("JIRA_EMAIL")
    token = _env("JIRA_API_TOKEN")
    project = _env("JIRA_PROJECT_KEY")

    missing = [
        name
        for name, value in (
            ("JIRA_BASE_URL", base),
            ("JIRA_EMAIL", email),
            ("JIRA_API_TOKEN", token),
            ("JIRA_PROJECT_KEY", project),
        )
        if not value
    ]
    if missing:
        return [CheckResult("jira", "凭据", False, "缺少环境变量：" + "、".join(missing))]

    results: list[CheckResult] = []
    try:
        with httpx.Client(base_url=base, timeout=timeout, auth=(email, token)) as client:
            resp = client.get(f"/rest/api/3/project/{project}")
            if resp.status_code >= 400:
                return [CheckResult("jira", "项目", False, f"HTTP {resp.status_code}：{resp.text[:120]}")]
            data = resp.json() or {}
            results.append(CheckResult("jira", "项目", True, f"{data.get('key')} · {data.get('name', '')}"))

            resp = client.get("/rest/api/3/issue/createmeta", params={"projectKeys": project, "expand": "projects.issuetypes"})
            if resp.status_code >= 400:
                results.append(CheckResult("jira", "缺陷类型", False, f"HTTP {resp.status_code}：{resp.text[:120]}"))
                return results
            projects = (resp.json() or {}).get("projects") or []
            types = [t.get("name") for p in projects for t in (p.get("issuetypes") or [])]
            ok = "Bug" in types
            results.append(
                CheckResult("jira", "缺陷类型", ok, f"{'Bug 可用' if ok else '缺少 Bug 类型'}（现有：{', '.join(types) or '空'}）")
            )
    except Exception as exc:  # noqa: BLE001
        results.append(CheckResult("jira", "连通性", False, f"{type(exc).__name__}: {exc}"))
    return results


def run(trackers: list[str], mock: bool = False) -> list[CheckResult]:
    if mock:
        from .e2e import MockTrackers

        with MockTrackers(_mock_dir()) as server:
            server.apply_env()
            return [item for name in trackers for item in _check(name)]
    return [item for name in trackers for item in _check(name)]


def _check(name: str) -> list[CheckResult]:
    if name == "zentao":
        return check_zentao()
    if name == "jira":
        return check_jira()
    return [CheckResult(name, "配置", False, f"未知缺陷系统：{name}")]


def _mock_dir():
    from .catalog import ROOT

    path = ROOT / "atf" / "e2e-runs" / "_tracker-check"
    path.mkdir(parents=True, exist_ok=True)
    return path
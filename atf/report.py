"""运行报告：把一次执行讲清楚。

三份产物：
- `results.json`：机器读（含每条用例的绑定关系与结果）
- `summary.md`：人读（结论 + 失败明细 + 覆盖率变化 + 缺陷流动作）
- `junit.xml`：CI 读（原始证据）
"""

from __future__ import annotations

import json
from pathlib import Path

from .catalog import ROOT, Catalog
from .runner import RunResult


def build_payload(result: RunResult, catalog: Catalog, filing: list | None = None) -> dict:
    counts = result.counts()
    coverage = catalog.coverage()
    return {
        "run_id": result.run_id,
        "started_at": result.started_at,
        "finished_at": result.finished_at,
        "duration_s": round(result.duration_s, 2),
        "returncode": result.returncode,
        "counts": counts,
        "cases": [
            {
                "case_id": item.case_id,
                "nodeid": item.nodeid,
                "status": item.status,
                "duration_s": round(item.duration, 3),
                "message": item.message[:300],
            }
            for item in result.outcomes
        ],
        "unbound_tests": result.unbound,
        "coverage": coverage,
        "filing": [item.__dict__ for item in (filing or [])],
    }


def write(result: RunResult, catalog: Catalog, filing: list | None = None) -> Path:
    run_dir = ROOT / result.run_dir
    run_dir.mkdir(parents=True, exist_ok=True)
    payload = build_payload(result, catalog, filing)

    (run_dir / "results.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n"
    )

    counts = payload["counts"]
    ok = counts["fail"] == 0 and counts["error"] == 0
    lines = [
        f"# 运行报告 · {result.run_id}",
        "",
        f"- 时间：{result.started_at} → {result.finished_at}（{result.duration_s:.1f}s）",
        f"- 结论：{'通过' if ok else '未通过'}（pass {counts['pass']} / fail {counts['fail']} / "
        f"error {counts['error']} / skip {counts['skip']}）",
        f"- 自动化覆盖率：{payload['coverage']['overall']['rate']:.0%}"
        f"（{payload['coverage']['overall']['automated']}/{payload['coverage']['overall']['total']}）",
        f"- 已绑定测试：{payload['coverage']['bound_tests']} / 收集到 {payload['coverage']['collected_tests']}",
        "",
    ]

    if payload["coverage"]["by_layer"]:
        lines += ["## 分层覆盖", "", "| 层级 | 用例 | 已自动化 | 比例 |", "|---|---|---|---|"]
        for layer, stat in payload["coverage"]["by_layer"].items():
            if stat["total"]:
                lines.append(f"| {layer} | {stat['total']} | {stat['automated']} | {stat['rate']:.0%} |")
        lines.append("")

    if result.failures:
        lines += ["## 失败明细", "", "| 用例 | 测试 | 状态 | 摘要 |", "|---|---|---|---|"]
        for item in result.failures:
            lines.append(
                f"| {item.case_id or '（黑户）'} | `{item.nodeid}` | {item.status} | "
                f"{item.message.splitlines()[0][:90] if item.message else ''} |"
            )
        lines.append("")

    if filing:
        lines += ["## 缺陷流", "", "| 用例 | 动作 | 缺陷系统 | 单号 | 说明 |", "|---|---|---|---|---|"]
        for item in filing:
            lines.append(
                f"| {item.case_id or '-'} | {item.action} | {item.tracker or '-'} | "
                f"{item.key or '-'} | {item.detail[:80]} |"
            )
        lines.append("")

    if result.unbound:
        lines += ["## 黑户测试（有用例代码但无用例库条目）", ""]
        lines += [f"- `{nodeid}`" for nodeid in result.unbound[:20]]
        lines.append("")

    (run_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return run_dir
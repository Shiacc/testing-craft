"""结果存储与报告层。

平台的价值有一半在这里：**同一份结果，能被三种人看懂**。
- 工程师看失败明细（哪个套件、哪个分片、失败信息）；
- 负责人看门禁结论（能不能发）；
- 管理者看趋势（这个月在变好还是变差）。

因此产物有三样：results.json（机器读）、report.html（人读）、history.jsonl（趋势）。
"""

from __future__ import annotations

import html
import json
from datetime import datetime
from pathlib import Path

from .runner import TaskResult

RUNS_DIR = Path(__file__).resolve().parents[1] / "runs"
HISTORY_FILE = RUNS_DIR / "history.jsonl"

STATUS_COLOR = {"PASS": "#0f9d58", "FAIL": "#c0392b", "TIMEOUT": "#d97706", "ERROR": "#8e44ad"}


def _sparkline(values: list[float], width: int = 160, height: int = 28) -> str:
    if not values:
        return ""
    top = max(values) or 1.0
    step = width / max(1, len(values) - 1)
    points = " ".join(
        f"{i * step:.1f},{height - (value / top) * (height - 4) - 2:.1f}" for i, value in enumerate(values)
    )
    return (
        f'<svg width="{width}" height="{height}" role="img" aria-label="耗时趋势">'
        f'<polyline fill="none" stroke="#2563eb" stroke-width="2" points="{points}"/></svg>'
    )


def _history_rows(limit: int = 10) -> list[dict]:
    if not HISTORY_FILE.exists():
        return []
    rows = []
    for line in HISTORY_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows[-limit:]


def append_history(record: dict) -> None:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    with HISTORY_FILE.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def write_results(run_dir: Path, payload: dict) -> Path:
    path = run_dir / "results.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def render_html(payload: dict) -> str:
    gate = payload["gate"]
    rows = []
    for result in payload["results"]:
        color = STATUS_COLOR.get(result["status"], "#646a73")
        rows.append(
            "<tr>"
            f"<td>{html.escape(result['suite_id'])}</td>"
            f"<td>{result['shard']}/{payload['shard_count']}</td>"
            f"<td><span style=\"color:{color};font-weight:600\">{result['status']}</span></td>"
            f"<td>{result['tests']}</td>"
            f"<td>{result['failures'] + result['errors']}</td>"
            f"<td>{result['skipped']}</td>"
            f"<td>{result['duration_s']:.2f}s</td>"
            f"<td>{html.escape(result['message'])[:120]}</td>"
            "</tr>"
        )

    history = [item for item in _history_rows(12) if item.get("run_id") != payload["run_id"]]
    durations = [item.get("wall_s", 0.0) for item in history] + [payload["gate"]["wall_s"]]
    trend_rows = "".join(
        f"<tr><td>{html.escape(str(item.get('run_id')))}</td>"
        f"<td>{item.get('tests', 0)}</td>"
        f"<td>{item.get('failed', 0)}</td>"
        f"<td>{'通过' if item.get('passed') else '未通过'}</td>"
        f"<td>{item.get('wall_s', 0):.1f}s</td></tr>"
        for item in history[-8:]
    )

    banner_color = "#0f9d58" if gate["passed"] else "#c0392b"
    reasons = "".join(f"<li>{html.escape(reason)}</li>" for reason in gate["reasons"]) or "<li>无</li>"

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>测试平台报告 · {payload['run_id']}</title>
<style>
 body{{font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;margin:32px;color:#1f2329;background:#fafbfc}}
 h1{{font-size:20px;margin:0 0 6px}} .meta{{color:#646a73;font-size:13px;margin-bottom:22px}}
 .banner{{background:{banner_color};color:#fff;padding:14px 18px;border-radius:8px;margin-bottom:22px}}
 table{{border-collapse:collapse;width:100%;background:#fff;font-size:13px;margin-bottom:26px}}
 th,td{{border:1px solid #dee0e3;padding:7px 10px;text-align:left}} th{{background:#f2f3f5}}
 .card{{background:#fff;border:1px solid #dee0e3;border-radius:8px;padding:16px 18px;margin-bottom:22px}}
 code{{background:#f2f3f5;padding:1px 4px;border-radius:3px}}
</style></head><body>
<h1>测试平台运行报告</h1>
<div class="meta">run_id <code>{payload['run_id']}</code> · 环境 {payload['environment']} ·
 并行度 {payload['parallel']} · 分片 {payload['shard_count']} · 标签 {payload['tag'] or '全部'} ·
 开始 {payload['started_at']}</div>

<div class="banner"><strong>门禁结论：{'通过' if gate['passed'] else '未通过'}</strong>
 &nbsp;|&nbsp; 用例 {gate['tests']}，失败 {gate['failed']}，通过率 {gate['pass_rate']:.2%}，
 墙面耗时 {gate['wall_s']:.1f}s</div>

<div class="card"><strong>未通过原因</strong><ul>{reasons}</ul></div>

<table><thead><tr>
<th>套件</th><th>分片</th><th>状态</th><th>用例</th><th>失败</th><th>跳过</th><th>耗时</th><th>失败信息</th>
</tr></thead><tbody>{''.join(rows)}</tbody></table>

<div class="card"><strong>耗时趋势（最近 {len(durations)} 次）</strong><br>{_sparkline(durations)}</div>

<table><thead><tr><th>run_id</th><th>用例</th><th>失败</th><th>门禁</th><th>墙面耗时</th></tr></thead>
<tbody>{trend_rows}</tbody></table>
</body></html>"""


def write_report(run_dir: Path, payload: dict) -> Path:
    path = run_dir / "report.html"
    path.write_text(render_html(payload), encoding="utf-8")
    return path


def summarize(results: list[TaskResult]) -> None:
    print("\n== 结果 ==")
    for result in sorted(results, key=lambda r: (r.suite_id, r.shard)):
        print(
            f"  [{result.status:<7}] {result.suite_id}#{result.shard}  "
            f"用例 {result.tests:>2}  失败 {result.failures + result.errors}  {result.duration_s:>5.2f}s"
            + (f"  {result.message[:60]}" if result.message else "")
        )
    print(f"  （生成于 {datetime.now().strftime('%H:%M:%S')}）")
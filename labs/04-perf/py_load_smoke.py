"""L5 性能实验：把性能测试做成"可进 CI 的阈值门禁"。

与常见的"压测出报告给人看"不同，本脚本的产物是 **退出码**：
超过阈值就 exit 1，CI 直接红。报告只是副产品。

用法：
    python py_load_smoke.py                        # 默认 20 并发 × 共 400 次请求
    python py_load_smoke.py --concurrency 50 --requests 2000
    python py_load_smoke.py --p95 80 --max-error-rate 0.001   # 收紧门禁

设计要点：
1. 目标是 labs/02 的订单服务（uvicorn 在后台线程启动，端口随机）；
2. 只测"试算"这类无副作用接口 —— 压测**绝不能**打会产生真实数据的写接口；
3. 指标：RPS、p50/p95/p99、错误率；阈值判定与报告分离，便于不同环境给不同阈值。
"""

from __future__ import annotations

import argparse
import statistics
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import uvicorn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "02-api-contract"))

from app.main import app  # noqa: E402


def _serve() -> tuple[uvicorn.Server, str]:
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    for _ in range(100):
        if server.started and server.servers:
            port = server.servers[0].sockets[0].getsockname()[1]
            return server, f"http://127.0.0.1:{port}"
        time.sleep(0.05)
    raise RuntimeError("服务未能在 5 秒内启动")


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(pct / 100 * len(ordered) + 0.5)) - 1))
    return ordered[index]


def run_load(base_url: str, concurrency: int, total: int) -> dict:
    """并发发起 total 次请求，返回延迟分布与错误统计（毫秒）。"""
    headers = {"Authorization": "Bearer tok-alice"}
    payload = {"items": [{"sku": "SKU-1", "unit_price": "100.00", "quantity": 2}]}
    per_worker = max(1, total // concurrency)

    latencies: list[float] = []
    errors: list[str] = []
    lock = threading.Lock()

    def worker() -> None:
        local_lat: list[float] = []
        local_err: list[str] = []
        with httpx.Client(base_url=base_url, timeout=10.0) as client:
            for _ in range(per_worker):
                started = time.perf_counter()
                try:
                    resp = client.post("/api/quote", json=payload, headers=headers)
                except Exception as exc:  # noqa: BLE001
                    local_err.append(type(exc).__name__)
                    continue
                local_lat.append((time.perf_counter() - started) * 1000)
                if resp.status_code != 200:
                    local_err.append(f"HTTP {resp.status_code}")
        with lock:
            latencies.extend(local_lat)
            errors.extend(local_err)

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        list(pool.map(lambda _: worker(), range(concurrency)))
    elapsed = time.perf_counter() - started

    return {
        "requests": len(latencies) + len(errors),
        "elapsed_s": elapsed,
        "rps": (len(latencies) + len(errors)) / elapsed if elapsed else 0.0,
        "p50": _percentile(latencies, 50),
        "p95": _percentile(latencies, 95),
        "p99": _percentile(latencies, 99),
        "mean": statistics.fmean(latencies) if latencies else 0.0,
        "error_rate": len(errors) / max(1, len(latencies) + len(errors)),
        "error_samples": sorted(set(errors))[:5],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="订单服务性能冒烟 + 阈值门禁")
    parser.add_argument("--concurrency", type=int, default=20, help="并发客户端数")
    parser.add_argument("--requests", type=int, default=400, help="总请求数")
    parser.add_argument("--p95", type=float, default=200.0, help="p95 阈值（毫秒）")
    parser.add_argument("--p99", type=float, default=500.0, help="p99 阈值（毫秒）")
    parser.add_argument("--max-error-rate", type=float, default=0.01, help="错误率阈值")
    parser.add_argument("--min-rps", type=float, default=20.0, help="最低吞吐阈值")
    args = parser.parse_args(argv)

    server, base_url = _serve()
    try:
        metrics = run_load(base_url, args.concurrency, args.requests)
    finally:
        server.should_exit = True

    checks = [
        ("p95", metrics["p95"], args.p95, "ms"),
        ("p99", metrics["p99"], args.p99, "ms"),
        ("错误率", metrics["error_rate"] * 100, args.max_error_rate * 100, "%"),
        ("RPS", metrics["rps"], args.min_rps, "req/s"),
    ]

    print("\n===== 性能结果 =====")
    print(f"请求数 {metrics['requests']}（并发 {args.concurrency}）  耗时 {metrics['elapsed_s']:.2f}s")
    print(
        f"延迟 mean={metrics['mean']:.1f}ms  p50={metrics['p50']:.1f}ms  "
        f"p95={metrics['p95']:.1f}ms  p99={metrics['p99']:.1f}ms"
    )
    print(f"吞吐 {metrics['rps']:.1f} req/s   错误率 {metrics['error_rate'] * 100:.2f}%")
    if metrics["error_samples"]:
        print(f"错误样例: {metrics['error_samples']}")

    print("\n===== 门禁判定（RPS 为下限，其余为上限）=====")
    failed = 0
    for name, actual, limit, unit in checks:
        lower_is_better = name not in ("RPS",)
        ok = actual <= limit if lower_is_better else actual >= limit
        arrow = "<=" if lower_is_better else ">="
        print(f"[{'PASS' if ok else 'FAIL'}] {name:<6} 实际 {actual:10.2f}{unit}  要求 {arrow} {limit}{unit}")
        failed += 0 if ok else 1

    if failed:
        print(f"\n门禁未通过：{failed} 项超阈值。")
        return 1
    print("\n门禁通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
// k6 版本的同一条性能底线（对照 labs/04/py_load_smoke.py）。
//
// 为什么同时给两版：
//   - k6 是 Go 写的独立二进制，单机压测吞吐远高于 Python 线程模型，适合"真压"；
//   - Python 版本零依赖、可直接在 CI 里跑，适合"每次提交都拦一道"的性能冒烟。
//
// 运行：
//   k6 run -e BASE_URL=http://127.0.0.1:8000 k6/load_test.js
//   k6 run --summary-export=artifacts/k6-summary.json k6/load_test.js
//
// 关键点：thresholds 不是"参考值"，是**门禁**。任一阈值不满足，k6 以非 0 退出，
// 流水线随即阻断。这比"跑完出一张漂亮的图没人看"有用得多。

import http from 'k6/http';
import { check, group, sleep } from 'k6';
import { Rate, Trend } from 'k6/metrics';

const BASE_URL = __ENV.BASE_URL || 'http://127.0.0.1:8000';
const TOKEN = __ENV.TOKEN || 'tok-alice';

const quoteLatency = new Trend('quote_latency', true);
const businessErrors = new Rate('business_errors');

export const options = {
  scenarios: {
    // 场景一：基线 —— 稳态下确认系统健康，用于每次发布的快速体检
    baseline: {
      executor: 'constant-vus',
      vus: 10,
      duration: '30s',
      tags: { scenario: 'baseline' },
    },
    // 场景二：峰值 —— 模拟活动开盘，确认限流与降级策略生效
    peak: {
      executor: 'ramping-vus',
      startTime: '35s',
      startVUs: 5,
      stages: [
        { duration: '20s', target: 60 },
        { duration: '40s', target: 60 },
        { duration: '20s', target: 5 },
      ],
      tags: { scenario: 'peak' },
    },
  },
  thresholds: {
    // 全局底线
    http_req_failed: ['rate<0.01'],            // 错误率 < 1%
    http_req_duration: ['p(95)<200', 'p(99)<500'],
    quote_latency: ['p(95)<180'],
    business_errors: ['rate<0.005'],
    // 峰值场景放宽但必须仍可用
    'http_req_duration{scenario:peak}': ['p(95)<400'],
  },
};

const PAYLOAD = JSON.stringify({
  items: [{ sku: 'SKU-1', unit_price: '100.00', quantity: 2 }],
});

const PARAMS = {
  headers: {
    'Content-Type': 'application/json',
    Authorization: `Bearer ${TOKEN}`,
  },
  tags: { api: 'quote' },
};

export default function () {
  group('试算接口', () => {
    const started = Date.now();
    const res = http.post(`${BASE_URL}/api/quote`, PAYLOAD, PARAMS);
    quoteLatency.add(Date.now() - started);

    const ok = check(res, {
      '状态码 200': (r) => r.status === 200,
      '返回应付金额': (r) => /"payable"/.test(r.body || ''),
      '延迟 < 500ms': (r) => r.timings.duration < 500,
    });

    // 业务错误单独计数：HTTP 200 但业务失败，是最容易被漏掉的一类问题
    if (!ok) businessErrors.add(1);
  });

  group('健康检查', () => {
    check(http.get(`${BASE_URL}/health`), { '服务存活': (r) => r.status === 200 });
  });

  sleep(0.1);
}
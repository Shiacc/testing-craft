"""本地 mock 缺陷服务：禅道 + Jira 的最小端点集合（仅标准库）。

为什么要有 mock 这条路径（而不是等真实账号）：
缺陷联动属于"**只有在真实系统里才会暴露问题**"的集成代码——认证头拼错、字段名写错、
状态码处理不对，都要打到真接口才知道。可仓库里没有禅道/Jira 账号，CI 也不该依赖外网。
mock 服务把这些协议细节**固化成可断言的期望**：请求打到本地 127.0.0.1，测的是
"客户端发出的请求长什么样"（才是这层代码的全部职责），而不是"对方系统能不能用"。
它同时是手工调试的入口（``python -m atf.bugfiler.mock_server --port 8765``）。

为什么 4xx 也要能模拟：
链路上最常见的真实故障是 401（token 过期）与 400（字段被校验掉）。只有能主动注入
失败状态，才能验证"失败会被包装成 BugFilerError、带上状态码、且不泄露凭据"。
没有这条路径，"错误处理"就是一段永远跑不到的死代码。

实现取舍：只用 ``http.server.ThreadingHTTPServer`` + 手写路由表，不引入 Flask/FastAPI。
mock 服务是**测试夹具**，多一个依赖就多一处 CI 装包成本与版本漂移风险。
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Self
from urllib.parse import urlsplit

ZENTAO_TOKEN_PATH = "/api.php/v1/tokens"
ZENTAO_BUG_PATH = "/api.php/v1/bugs"
JIRA_ISSUE_PATH = "/rest/api/3/issue"
HEALTH_PATH = "/healthz"
RECORDS_PATH = "/__requests"
MAX_BODY_BYTES = 1 << 20  # 单请求体上限：mock 不该被一个 2GB 的 POST 拖死


@dataclass
class MockConfig:
    """mock 服务的行为开关与记录区（handler 与门面共享同一个实例）。"""

    token: str = "mock-token"
    zen_bug_id: int = 101
    jira_key: str = "QA-1"
    jira_issue_id: str = "10001"
    require_auth: bool = True
    fail_routes: dict[str, int] = field(default_factory=dict)
    records: list[dict] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)
    base_url: str = ""
    quiet: bool = True


# ---------------------------------------------------------------- 路由处理


def _zen_tokens(cfg: MockConfig, entry: dict) -> tuple[int, dict]:
    body = entry["body"] if isinstance(entry["body"], dict) else {}
    account = str(body.get("account") or "").strip()
    password = str(body.get("password") or "").strip()
    if not account or not password:
        return 401, {"error": "account/password 不能为空"}
    return 200, {"token": cfg.token}


def _zen_bugs(cfg: MockConfig, entry: dict) -> tuple[int, dict]:
    body = entry["body"] if isinstance(entry["body"], dict) else {}
    if cfg.require_auth and entry["headers"].get("token") != cfg.token:
        return 401, {"error": "Token 缺失或无效"}
    if not str(body.get("title") or "").strip():
        return 400, {"error": "title 不能为空"}
    return 200, {"id": cfg.zen_bug_id, "title": body.get("title", ""), "status": "active"}


def _jira_issue(cfg: MockConfig, entry: dict) -> tuple[int, dict]:
    auth = str(entry["headers"].get("authorization") or "")
    if cfg.require_auth and not auth.lower().startswith("basic "):
        return 401, {"errorMessages": ["需要 Basic 认证"]}
    body = entry["body"] if isinstance(entry["body"], dict) else {}
    fields = body.get("fields")
    if not isinstance(fields, dict):
        return 400, {"errorMessages": ["缺少 fields 对象"]}
    for required in ("project", "summary", "issuetype"):
        if required not in fields:
            return 400, {"errorMessages": [f"缺少必填字段 {required}"]}
    return 201, {
        "id": cfg.jira_issue_id,
        "key": cfg.jira_key,
        "self": f"{cfg.base_url}{JIRA_ISSUE_PATH}/{cfg.jira_issue_id}",
    }


ROUTES = {
    ZENTAO_TOKEN_PATH: _zen_tokens,
    ZENTAO_BUG_PATH: _zen_bugs,
    JIRA_ISSUE_PATH: _jira_issue,
}


# ---------------------------------------------------------------- HTTP handler


class _MockHandler(BaseHTTPRequestHandler):
    """把每个请求（方法/路径/头/体）记录到 ``self.server.config.records``。

    头字段一律**小写化**后记录：httpx 与 http.server 对 header 大小写处理不一致，
    测试里断言 ``headers["token"]`` 时不该被这种实现细节绊倒。
    """

    protocol_version = "HTTP/1.1"  # 需要准确 Content-Length（_send 保证）
    server_version = "atf-mock/1.0"
    sys_version = ""

    @property
    def config(self) -> MockConfig:
        return self.server.config  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: object) -> None:  # 覆盖基类签名，参数名不能改
        if not self.config.quiet:
            sys.stderr.write(f"[mock] {format % args}\n")

    # ------------------------------------------------------------ 记录

    def _read_body(self) -> object:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        length = max(0, min(length, MAX_BODY_BYTES))
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return None
        text = raw.decode("utf-8", errors="replace")
        content_type = str(self.headers.get("Content-Type") or "")
        if "json" in content_type.lower():
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return text
        return text

    def _record(self, method: str, path: str, body: object) -> dict:
        cfg = self.config
        entry = {
            "seq": len(cfg.records) + 1,
            "method": method,
            "path": path,
            "headers": {key.lower(): value for key, value in self.headers.items()},
            "body": body,
            "received_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        with cfg.lock:
            entry["seq"] = len(cfg.records) + 1  # 锁内重编号，避免并发下 seq 重复
            cfg.records.append(entry)
        return entry

    # ------------------------------------------------------------ 响应

    def _send(self, status: int, payload: object) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # ------------------------------------------------------------ 分发

    def do_POST(self) -> None:  # 基类约定的方法名（do_<METHOD>），不能改成别的名字
        path = urlsplit(self.path).path.rstrip("/") or "/"
        body = self._read_body()
        entry = self._record("POST", path, body)

        injected = self.config.fail_routes.get(path)
        if injected:
            self._send(injected, {"error": f"mock 注入的失败状态 {injected}", "path": path})
            return
        route = ROUTES.get(path)
        if route is None:
            self._send(404, {"error": "未实现的端点", "path": path})
            return
        try:
            status, payload = route(self.config, entry)
        except Exception as exc:  # mock 内部出错要变成可读的 500，而不是直接断连接
            self._send(500, {"error": f"{type(exc).__name__}: {exc}"})
            return
        self._send(status, payload)

    def do_GET(self) -> None:  # 基类约定的方法名（do_<METHOD>），不能改成别的名字
        path = urlsplit(self.path).path.rstrip("/") or "/"
        if path == HEALTH_PATH:
            self._send(200, {"status": "ok", "endpoints": sorted(ROUTES)})
            return
        if path == RECORDS_PATH:
            # 调试端点：手工起 mock 后可以直接看"客户端到底发了什么"。
            with self.config.lock:
                self._send(200, {"count": len(self.config.records), "records": list(self.config.records)})
            return
        self._send(404, {"error": "未实现的端点", "path": path})


class _MockHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], config: MockConfig) -> None:
        self.config = config
        super().__init__(address, _MockHandler)


# ---------------------------------------------------------------- 门面


class ThreadedMockServer:
    """测试用上下文管理器：``with ThreadedMockServer() as server: server.base_url``。

    后台线程跑 serve_forever，``port=0`` 时由操作系统分配空闲端口，多个测试并行不会撞端口。
    """

    def __init__(
        self,
        port: int = 0,
        *,
        host: str = "127.0.0.1",
        token: str = "mock-token",
        jira_key: str = "QA-1",
        zen_bug_id: int = 101,
        fail_routes: dict[str, int] | None = None,
        require_auth: bool = True,
        quiet: bool = True,
    ) -> None:
        self.config = MockConfig(
            token=token,
            zen_bug_id=int(zen_bug_id),
            jira_key=jira_key,
            require_auth=require_auth,
            fail_routes=dict(fail_routes or {}),
            quiet=quiet,
        )
        self._host = host
        self._requested_port = int(port)
        self._httpd: _MockHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------ 生命周期

    def start(self) -> Self:
        if self._httpd is not None:
            return self
        self._httpd = _MockHTTPServer((self._host, self._requested_port), self.config)
        host, port = self._httpd.server_address[0], self._httpd.server_address[1]
        self.config.base_url = f"http://{host}:{port}"
        # poll_interval 调小：shutdown() 的等待上限就是这个值，测试里几十个服务实例叠起来很可观。
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            kwargs={"poll_interval": 0.02},
            name="atf-mock",
            daemon=True,
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        httpd, thread = self._httpd, self._thread
        self._httpd, self._thread = None, None
        if httpd is None:
            return
        httpd.shutdown()
        httpd.server_close()
        if thread is not None:
            thread.join(timeout=5.0)

    def serve_forever(self) -> None:
        """给 ``python -m atf.bugfiler.mock_server`` 用：阻塞直到 KeyboardInterrupt。"""
        if self._httpd is None:
            self.start()
        assert self._httpd is not None
        self._httpd.serve_forever()

    def __enter__(self) -> Self:
        return self.start()

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.stop()

    # ------------------------------------------------------------ 断言用接口

    @property
    def base_url(self) -> str:
        if not self.config.base_url:
            raise RuntimeError("mock 服务尚未 start()，base_url 不可用")
        return self.config.base_url

    @property
    def port(self) -> int:
        if self._httpd is None:
            return self._requested_port
        return int(self._httpd.server_address[1])

    @property
    def records(self) -> list[dict]:
        """全部已收到请求的副本（顺序即到达顺序）。"""
        with self.config.lock:
            return [dict(record) for record in self.config.records]

    @property
    def requests(self) -> list[dict]:
        """``records`` 的别名，迎合"requests 列表"这种习惯叫法。"""
        return self.records

    def find(self, path: str) -> list[dict]:
        """按端点取记录（只匹配路径，忽略 query）。"""
        target = path.rstrip("/") or "/"
        return [record for record in self.records if record["path"] == target]

    def last(self, path: str) -> dict | None:
        matched = self.find(path)
        return matched[-1] if matched else None

    def clear(self) -> None:
        with self.config.lock:
            self.config.records.clear()

    def __repr__(self) -> str:
        state = "running" if self._httpd is not None else "stopped"
        return f"ThreadedMockServer({self.config.base_url or 'stopped'}，{state}，records={len(self.config.records)})"


# ---------------------------------------------------------------- CLI


def _parse_fail_routes(values: list[str]) -> dict[str, int]:
    """``--fail /api.php/v1/bugs=400`` → {"/api.php/v1/bugs": 400}。"""
    routes: dict[str, int] = {}
    for value in values:
        path, _, status = value.partition("=")
        path = path.strip()
        try:
            code = int(status)
        except ValueError as exc:
            raise SystemExit(f"--fail 需要 <路径>=<状态码> 形式，收到 {value!r}") from exc
        if not path.startswith("/"):
            raise SystemExit(f"--fail 的路径必须以 / 开头，收到 {path!r}")
        routes[path.rstrip("/") or "/"] = code
    return routes


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="atf.bugfiler.mock_server",
        description="本地 mock 缺陷服务（禅道 + Jira 端点），供手工调试与测试使用",
    )
    parser.add_argument("--port", type=int, default=8765, help="监听端口（0 = 随机空闲端口）")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址，默认只绑回环")
    parser.add_argument("--token", default="mock-token", help="mock 认可的禅道 token")
    parser.add_argument(
        "--fail",
        action="append",
        default=[],
        metavar="PATH=STATUS",
        help="给某个端点注入失败状态，如 --fail /api.php/v1/bugs=400（可重复）",
    )
    parser.add_argument("--verbose", action="store_true", help="打印每个请求的访问日志")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    server = ThreadedMockServer(
        args.port,
        host=args.host,
        token=args.token,
        fail_routes=_parse_fail_routes(args.fail),
        quiet=not args.verbose,
    )
    with server:
        print(f"mock 缺陷服务已启动：{server.base_url}")
        print(f"  禅道令牌  POST {ZENTAO_TOKEN_PATH}")
        print(f"  禅道缺陷  POST {ZENTAO_BUG_PATH}")
        print(f"  Jira 缺陷 POST {JIRA_ISSUE_PATH}")
        print(f"  健康检查  GET  {HEALTH_PATH}    请求记录 GET {RECORDS_PATH}")
        print("Ctrl+C 停止")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\n已停止")
    return 0


if __name__ == "__main__":
    sys.exit(main())

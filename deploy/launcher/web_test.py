"""单体前端（静态 + /api 反代）自检：只测纯逻辑与真实 HTTP 往返，用回环随机端口。

不启动任何项目服务：上游用临时起的假 gateway（标准库），前端也用 launcher/web.py 起的真进程。
"""

from __future__ import annotations

import http.server
import json
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "deploy"))

from launcher import plan as planner  # noqa: E402
from launcher import web  # noqa: E402

FAILS: list[str] = []
CHECKS = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global CHECKS
    CHECKS += 1
    if cond:
        print(f"  ok    {name}")
    else:
        FAILS.append(name)
        print(f"  FAIL  {name} {extra}")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_port(port: int, timeout: float = 10.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                return True
        except OSError:
            time.sleep(0.1)
    return False


# ── 假上游：/api/echo 回 JSON，/api/events 发两帧 SSE ──────────
class FakeGateway(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # noqa: A003
        pass

    def do_GET(self):  # noqa: N802
        if self.path.startswith("/api/events-slow"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()

            def send(payload: bytes) -> None:
                self.wfile.write(f"{len(payload):X}\r\n".encode() + payload + b"\r\n")
                self.wfile.flush()

            send(b"data: first\n\n")
            time.sleep(1.5)          # 故意延迟第二帧：用来验证代理是否真"边收边转"
            send(b"data: second\n\n")
            self.wfile.write(b"0\r\n\r\n")
            return
        if self.path.startswith("/api/events"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for i in range(2):
                payload = f"data: {{\"n\":{i}}}\n\n".encode()
                self.wfile.write(f"{len(payload):X}\r\n".encode() + payload + b"\r\n")
                self.wfile.flush()
                time.sleep(0.05)
            self.wfile.write(b"0\r\n\r\n")
            return
        body = json.dumps({"ok": True, "path": self.path, "auth": self.headers.get("Authorization", "")}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        payload = self.rfile.read(length).decode("utf-8", "replace")
        body = json.dumps({"ok": True, "echo": payload}).encode()
        self.send_response(201)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def http_get(url: str, with_auth: bool = False, timeout: float = 5.0):
    request = urllib.request.Request(url)
    if with_auth:
        request.add_header("Authorization", "Bearer t0ken")
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        return resp.status, resp.read()


def main() -> int:
    tmp = Path(tempfile.mkdtemp())
    static_dir = tmp / "static"
    static_dir.mkdir()
    (static_dir / "login.html").write_text("<h1>login</h1>", encoding="utf-8")
    (static_dir / "app.js").write_text("console.log(1)", encoding="utf-8")
    (tmp / "secret.txt").write_text("SECRET", encoding="utf-8")

    print("路径解析（防目录穿越）")
    handler = web.FrontendHandler
    handler.static_root = static_dir
    request = type("R", (), {"static_root": static_dir})()
    check("正常文件可解析", web.FrontendHandler._resolve(request, "/login.html") == (static_dir / "login.html"))
    check("目录穿越被拒", web.FrontendHandler._resolve(request, "/../secret.txt") is None,
          str(web.FrontendHandler._resolve(request, "/../secret.txt")))
    check("编码穿越被拒", web.FrontendHandler._resolve(request, "/..%2fsecret.txt") is None)

    print("计划里的前端字段（core 含 gateway 时应启用）")
    plan = planner.build(ROOT, tmp / "data", ["core", "memory"])
    check("web_port 默认 8080", plan.web_port == 8080, str(plan.web_port))
    check("gateway_port 是 5210", plan.gateway_port == 5210, str(plan.gateway_port))
    check("to_dict 带 web 段", plan.to_dict()["web"]["port"] == 8080)
    plan_no_gw = planner.build(ROOT, tmp / "data2", ["memory"])
    check("没有 gateway 就没有前端", plan_no_gw.web_port == 0, str(plan_no_gw.web_port))

    print("真实 HTTP 往返（前端进程 + 假 gateway）")
    upstream_port = free_port()
    gateway = http.server.ThreadingHTTPServer(("127.0.0.1", upstream_port), FakeGateway)
    threading.Thread(target=gateway.serve_forever, daemon=True).start()

    front_port = free_port()
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "deploy" / "launcher" / "web.py"),
         "--port", str(front_port), "--static", str(static_dir),
         "--upstream", f"http://127.0.0.1:{upstream_port}"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        check("前端端口已就绪", wait_port(front_port), str(front_port))
        base = f"http://127.0.0.1:{front_port}"

        # / → 302 login.html
        no_redirect = urllib.request.build_opener(type("NoRedirect", (urllib.request.HTTPRedirectHandler,), {
            "redirect_request": lambda self, req, fp, code, msg, headers, newurl: None})())
        try:
            no_redirect.open(base + "/", timeout=5)
            check("/ 返回 302", False, "没有 302")
        except urllib.error.HTTPError as exc:
            check("/ 返回 302", exc.code == 302 and exc.headers.get("Location") == "/login.html",
                  f"{exc.code} {exc.headers.get('Location')}")

        status, body = http_get(base + "/login.html")
        check("静态 html 可访问", status == 200 and b"login" in body, f"{status}")
        check("静态 html 的 MIME", body == b"<h1>login</h1>")

        status, body = http_get(base + "/app.js")
        check("静态 js 可访问", status == 200 and b"console.log" in body)

        try:
            http_get(base + "/nope.html")
            check("不存在的文件 404", False)
        except urllib.error.HTTPError as exc:
            check("不存在的文件 404", exc.code == 404, str(exc.code))

        status, body = http_get(base + "/api/me", with_auth=True)
        data = json.loads(body)
        check("代理 GET 状态码", status == 200, str(status))
        check("代理透传路径", data["path"] == "/api/me", data["path"])
        check("代理透传 Authorization", data["auth"] == "Bearer t0ken", data["auth"])

        req = urllib.request.Request(base + "/api/login", data=b'{"u":1}', method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            check("代理 POST 状态码", resp.status == 201, str(resp.status))
            check("代理 POST 请求体", json.loads(resp.read())["echo"] == '{"u":1}')

        # 增量性：第一帧必须在第二帧发出前就到达客户端（read1 修复点）
        request = urllib.request.Request(base + "/api/events-slow")
        started = time.time()
        with urllib.request.urlopen(request, timeout=10) as resp:
            first = resp.read1(4096) if hasattr(resp, "read1") else resp.read(64)
            elapsed = time.time() - started
            rest = resp.read()
        check("第一帧在 1.5s 内到达（不是缓冲到最后）", elapsed < 1.2 and b"first" in first,
              f"elapsed={elapsed:.2f}s first={first[:40]!r}")
        check("第二帧也完整透传", b"second" in (first + rest), (first + rest)[:80])

        status, raw = http_get(base + "/api/events")
        check("SSE 事件透传", raw.count(b"data:") == 2, raw[:80])
        check("SSE 内容正确", b'"n":1' in raw, raw[:80])
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        gateway.shutdown()

    print()
    if FAILS:
        print(f"FAILED {len(FAILS)}: {FAILS}")
        return 1
    print(f"ALL OK — {CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

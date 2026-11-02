"""单体模式的前端：用标准库实现"静态文件 + /api 反向代理"（替代 nginx 容器）。

为什么不用 FastAPI：单体档要保证"只用 Python 就能跑"，这个进程必须能用**基础解释器**
启动（不依赖共享 venv 里装好的第三方包）。所以全部用 `http.server` + `urllib`。

对齐 nginx 的行为（见 services/frontend-service/nginx.conf）：
- `/` → 302 `/login.html`
- `/api/events` → 转发到 gateway，**关闭缓冲**（SSE 逐条透传）
- 其它 `/api/` → 转发到 gateway（保留 Authorization 等头、透传状态码与响应头）
- 其余路径 → 静态文件（login/chat 的 html/css/js + backgrounds 目录），目录穿越会被拒绝

用法：
  python -X utf8 deploy/launcher/web.py --port 8080 --static <frontend 目录> --upstream http://127.0.0.1:5210
"""

from __future__ import annotations

import argparse
import http.server
import mimetypes
import os
import socket
import sys
import urllib.error
import urllib.request
from pathlib import Path

MIME_OVERRIDE = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
}

# 不透传给上游的请求头（hop-by-hop）
SKIP_REQUEST_HEADERS = {
    "host", "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "content-length", "accept-encoding",
}
# 不回转给浏览器的响应头
SKIP_RESPONSE_HEADERS = {
    "connection", "keep-alive", "transfer-encoding", "content-encoding", "content-length",
}


class FrontendHandler(http.server.BaseHTTPRequestHandler):
    server_version = "MyAgentWeb/1.0"
    protocol_version = "HTTP/1.1"

    # 由 main() 注入
    static_root: Path = Path(".")
    upstream: str = "http://127.0.0.1:5210"

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        if os.getenv("MYAGENT_WEB_QUIET") == "1":
            return
        print(f"[web] {self.address_string()} {fmt % args}", flush=True)

    # ── 路由 ────────────────────────────────────────────────
    def do_GET(self) -> None:  # noqa: N802
        self._dispatch()

    def do_HEAD(self) -> None:  # noqa: N802
        self._dispatch(head_only=True)

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch()

    def do_PUT(self) -> None:  # noqa: N802
        self._dispatch()

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch()

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._dispatch()

    def _dispatch(self, head_only: bool = False) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/":
            self.send_response(302)
            self.send_header("Location", "/login.html")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path.startswith("/api/"):
            self._proxy(head_only=head_only)
            return
        self._serve_static(path, head_only=head_only)

    # ── 静态文件 ────────────────────────────────────────────
    def _resolve(self, url_path: str) -> Path | None:
        rel = urllib.request.url2pathname(url_path).lstrip("/\\")
        candidate = (self.static_root / rel).resolve()
        try:
            candidate.relative_to(self.static_root.resolve())
        except ValueError:
            return None      # 目录穿越
        return candidate

    def _serve_static(self, url_path: str, head_only: bool = False) -> None:
        target = self._resolve(url_path)
        if target is None:
            self._not_found()
            return
        if target.is_dir():
            target = target / "index.html"
        if not target.is_file():
            self._not_found()
            return
        try:
            data = target.read_bytes()
        except Exception as exc:  # noqa: BLE001
            self._send_text(500, f"读取文件失败：{exc}")
            return
        mime = MIME_OVERRIDE.get(target.suffix.lower()) or mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        if not head_only:
            self.wfile.write(data)

    def _not_found(self) -> None:
        body = b"404 Not Found"
        self.send_response(404)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, code: int, text: str) -> None:
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ── /api 反向代理 ───────────────────────────────────────
    def _read_body(self) -> bytes:
        length = self.headers.get("Content-Length")
        if not length:
            return b""
        try:
            return self.rfile.read(int(length))
        except Exception:  # noqa: BLE001
            return b""

    def _proxy(self, head_only: bool = False) -> None:
        url = self.upstream.rstrip("/") + self.path
        body = self._read_body() if self.command in {"POST", "PUT", "PATCH", "DELETE"} else None

        headers = {}
        for key, value in self.headers.items():
            if key.lower() in SKIP_REQUEST_HEADERS:
                continue
            headers[key] = value
        headers["Accept-Encoding"] = "identity"   # 不让上游压缩，便于逐块透传

        request = urllib.request.Request(url=url, data=body, headers=headers, method=self.command)
        try:
            response = urllib.request.urlopen(request, timeout=None)
        except urllib.error.HTTPError as exc:
            response = exc            # 4xx/5xx 也要把响应体转回去
        except Exception as exc:  # noqa: BLE001
            self._send_text(502, f"网关不可达：{exc}")
            return

        try:
            status = getattr(response, "status", None) or getattr(response, "code", 502)
            self.send_response(status)
            for key, value in (response.headers.items() if response.headers else []):
                if key.lower() in SKIP_RESPONSE_HEADERS:
                    continue
                self.send_header(key, value)
            # 未知长度：用 chunked 边读边写（SSE 需要）
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()

            if head_only:
                self._write_chunk(b"")
                return

            # 关键：必须用 read1()。read(n) 会一直阻塞到凑满 n 字节，
            # 而 SSE 一帧通常只有几十字节且永远不结束 → 浏览器什么都收不到。
            read_chunk = getattr(response, "read1", None) or response.read
            while True:
                chunk = read_chunk(4096)
                if not chunk:
                    break
                self._write_chunk(chunk)
                try:
                    self.wfile.flush()
                except Exception:  # noqa: BLE001
                    break
            self._write_chunk(b"")
        finally:
            try:
                response.close()
            except Exception:  # noqa: BLE001
                pass

    def _write_chunk(self, data: bytes) -> None:
        self.wfile.write(f"{len(data):X}\r\n".encode("ascii"))
        if data:
            self.wfile.write(data)
        self.wfile.write(b"\r\n")


class ReusableServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def _port_open(port: int, host: str = "127.0.0.1") -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.3):
            return True
    except OSError:
        return False


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="单体模式前端（静态 + /api 反代）")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--static", default="", help="frontend 静态目录（默认仓库 services/frontend-service）")
    parser.add_argument("--upstream", default="http://127.0.0.1:5210", help="gateway 地址")
    parser.add_argument("--backgrounds", default="", help="背景图目录（可选，挂到 /backgrounds）")
    args = parser.parse_args(argv[1:])

    repo = Path(__file__).resolve().parent.parent.parent
    static_root = Path(args.static) if args.static else repo / "services" / "frontend-service"
    if not static_root.is_dir():
        print(f"错误：静态目录不存在 {static_root}")
        return 2

    if _port_open(args.port):
        print(f"错误：端口 {args.port} 已被占用（前端可能已在运行）")
        return 1

    FrontendHandler.static_root = static_root
    FrontendHandler.upstream = args.upstream.rstrip("/")

    server = ReusableServer((args.host, args.port), FrontendHandler)
    print(f"[web] 静态目录 {static_root}", flush=True)
    print(f"[web] /api → {FrontendHandler.upstream}", flush=True)
    print(f"[web] 监听 http://{args.host}:{args.port}/login.html", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("[web] 停止", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

"""user-service 入口：同时启动 gRPC 和 HTTP 服务。"""

import json
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

from app import config
from app.server import serve as serve_grpc
from app.user_store import user_store


class KeyHandler(BaseHTTPRequestHandler):
    """轻量 HTTP 端点，供 context-service 存取 OpenViking API key。"""

    def do_GET(self):
        if self.path.startswith("/openviking_key/"):
            user_id = self.path.split("/openviking_key/", 1)[1]
            key = user_store.get_openviking_key(user_id)
            self._json(200, {"ok": key is not None, "user_id": user_id, "api_key": key or ""})
        else:
            self._json(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        if self.path.startswith("/openviking_key/"):
            user_id = self.path.split("/openviking_key/", 1)[1]
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
            api_key = body.get("api_key", "")
            ok = user_store.set_openviking_key(user_id, api_key)
            self._json(200, {"ok": ok, "user_id": user_id})
        else:
            self._json(404, {"ok": False, "error": "not found"})

    def _json(self, code: int, data: dict):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def log_message(self, format, *args):
        pass  # suppress HTTP access logs


def _run_http():
    port = int(config.USER_HTTP_PORT) if hasattr(config, "USER_HTTP_PORT") else 5204
    server = HTTPServer(("0.0.0.0", port), KeyHandler)
    print(f"user-service HTTP started on 0.0.0.0:{port}", flush=True)
    server.serve_forever()


def main():
    # 启动 HTTP 线程
    http_thread = threading.Thread(target=_run_http, daemon=True)
    http_thread.start()
    # 启动 gRPC（阻塞主线程）
    serve_grpc()


if __name__ == "__main__":
    main()

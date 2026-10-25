"""user-service 入口：同时启动 gRPC 和 HTTP 服务。

HTTP 端点（供其它服务与 dashboard 使用，gRPC 之外的低成本通道）：
  GET  /openviking_key/{user_id}         取用户的 OpenViking key
  POST /openviking_key/{user_id}         写用户的 OpenViking key
  GET  /channels/{channel}               列出某渠道的绑定（dashboard「QQ 设置」用）
  POST /channels/bind                    绑定渠道身份 {user_id, channel, channel_user_id, priority}
  POST /channels/unbind                  解绑 {user_id, channel}
"""

import json
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import unquote

from app import config
from app.server import serve as serve_grpc
from app.user_store import user_store


class KeyHandler(BaseHTTPRequestHandler):
    """轻量 HTTP 端点，供 context-service / dashboard 使用。"""

    def do_GET(self):
        if self.path.startswith("/openviking_key/"):
            user_id = self.path.split("/openviking_key/", 1)[1]
            key = user_store.get_openviking_key(user_id)
            self._json(200, {"ok": key is not None, "user_id": user_id, "api_key": key or ""})
        elif self.path.startswith("/channels/"):
            channel = unquote(self.path.split("/channels/", 1)[1].split("?", 1)[0]).strip("/")
            self._json(200, {"ok": True, "channel": channel, "bindings": self._bindings(channel)})
        else:
            self._json(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        if self.path.startswith("/openviking_key/"):
            user_id = self.path.split("/openviking_key/", 1)[1]
            body = self._body()
            api_key = body.get("api_key", "")
            ok = user_store.set_openviking_key(user_id, api_key)
            self._json(200, {"ok": ok, "user_id": user_id})
        elif self.path == "/channels/bind":
            body = self._body()
            message = user_store.bind_channel(
                str(body.get("user_id") or ""),
                str(body.get("channel") or ""),
                str(body.get("channel_user_id") or ""),
                int(body.get("priority") or 0),
            )
            self._json(200, {"ok": message == "ok", "message": message})
        elif self.path == "/channels/unbind":
            body = self._body()
            message = user_store.unbind_channel(
                str(body.get("user_id") or ""),
                str(body.get("channel") or ""),
            )
            self._json(200, {"ok": message == "ok", "message": message})
        else:
            self._json(404, {"ok": False, "error": "not found"})

    @staticmethod
    def _bindings(channel: str) -> list[dict]:
        """列出所有用户在某渠道上的绑定。

        `list_users()` 会把**别名指针**（channel_user_id 那个文件）也算作"用户"，
        而 `get_user(alias)` 透明返回规范记录 —— 直接遍历会把同一条绑定重复输出
        （一条绑定 + 两个别名 = 3 行）。这里只认规范记录，并按
        (user_id, channel_user_id) 去重兜底。
        """
        out: list[dict] = []
        seen: set[tuple[str, str]] = set()
        for item in user_store.list_users():
            user_id = str(item.get("user_id") or "")
            if not user_id:
                continue
            data = user_store.get_user(user_id) or {}
            canonical = str(data.get("user_id") or "")
            if canonical != user_id:
                continue  # 别名指针：真实记录在规范用户名下，已/将会被单独枚举
            channels = data.get("channels") or {}
            entry = channels.get(channel) if isinstance(channels, dict) else None
            if not isinstance(entry, dict):
                continue
            channel_user_id = str(entry.get("channel_user_id") or "").strip()
            if not channel_user_id:
                continue
            key = (canonical, channel_user_id)
            if key in seen:
                continue
            seen.add(key)
            # 绑定的"归属"在规范账户上（别名机制保证一人一记录），
            # 但框架侧用的是 qq 渠道身份（QQ 号），所以额外把它带出来给前端展示。
            qq_entry = channels.get("qq") if isinstance(channels, dict) else None
            qq_user_id = ""
            if isinstance(qq_entry, dict):
                qq_user_id = str(qq_entry.get("channel_user_id") or "").strip()
            out.append({
                "user_id": canonical,
                "qq_user_id": qq_user_id,
                "channel": channel,
                "channel_user_id": channel_user_id,
                "priority": int(entry.get("priority") or 0),
            })
        out.sort(key=lambda row: (row["qq_user_id"] or row["user_id"], row["channel_user_id"]))
        return out

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
        try:
            data = json.loads(self.rfile.read(length))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _json(self, code: int, data: dict):
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

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

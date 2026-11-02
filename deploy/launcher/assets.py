"""单体模式的图床（替代 k8s 里的 image-assets-service）。

k8s 里图床是独立容器（nginx 服务 `/assets/<file>`）；单体没有它，
所以这里用标准库起一个只读静态服务，暴露同样的 URL 形状：

    GET /assets/<file>   → <DATA_DIR>/assets/images/<file>
    GET /health          → {"ok":true}

只做 GET/HEAD，拒绝目录穿越；用基础解释器跑（不依赖 venv 里的包）。
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import socket
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

MIME_OVERRIDE = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".json": "application/json; charset=utf-8",
}


class AssetHandler(BaseHTTPRequestHandler):
    server_version = "MyAgentAssets/1.0"
    protocol_version = "HTTP/1.1"
    assets_dir: Path = Path("assets/images")

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        if os.getenv("MYAGENT_ASSETS_QUIET") == "1":
            return
        print(f"[assets] {self.address_string()} {fmt % args}", flush=True)

    def _send_json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _resolve(self, url_path: str) -> Path | None:
        # /assets/<name> → <assets_dir>/<name>
        rel = url_path[len("/assets/"):] if url_path.startswith("/assets/") else url_path.lstrip("/")
        from urllib.request import url2pathname

        rel = url2pathname(rel)
        candidate = (self.assets_dir / rel).resolve()
        try:
            candidate.relative_to(self.assets_dir.resolve())
        except ValueError:
            return None
        return candidate

    def do_HEAD(self) -> None:  # noqa: N802
        self._serve(head_only=True)

    def do_GET(self) -> None:  # noqa: N802
        self._serve(head_only=False)

    def _serve(self, head_only: bool) -> None:
        path = self.path.split("?", 1)[0]
        if path in {"/health", "/healthz"}:
            self._send_json(200, {"ok": True, "dir": str(self.assets_dir)})
            return
        if not path.startswith("/assets/"):
            self._send_json(404, {"error": "not found", "hint": "图片地址形如 /assets/<file>"})
            return

        target = self._resolve(path)
        if target is None or not target.is_file():
            self._send_json(404, {"error": f"asset not found: {path}"})
            return

        try:
            data = target.read_bytes()
        except Exception as exc:  # noqa: BLE001
            self._send_json(500, {"error": f"read failed: {exc}"})
            return

        mime = MIME_OVERRIDE.get(target.suffix.lower()) or mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=600")
        self.end_headers()
        if not head_only:
            self.wfile.write(data)


class ReusableServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="单体模式图床（/assets 静态服务）")
    parser.add_argument("--host", default=os.getenv("ASSETS_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("ASSETS_PORT", "5102")))
    parser.add_argument("--dir", default="", help="图片目录（默认 <DATA_DIR>/assets/images）")
    args = parser.parse_args(argv[1:])

    assets_dir = Path(args.dir) if args.dir else Path.cwd() / "assets" / "images"
    assets_dir.mkdir(parents=True, exist_ok=True)
    AssetHandler.assets_dir = assets_dir

    server = ReusableServer((args.host, args.port), AssetHandler)
    print(f"[assets] 目录 {assets_dir}", flush=True)
    print(f"[assets] 监听 http://{args.host}:{args.port}/assets/<file>", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("[assets] 停止", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

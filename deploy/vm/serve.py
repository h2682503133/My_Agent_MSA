"""虚拟机自测用的宿主 HTTP 服务：发文件 + 收回测试结果。

用途：Windows 虚拟机没有 Guest Additions 时，靠 NAT 里的 10.0.2.2 与宿主通信。
  GET  /repo.zip        项目压缩包（自动按需重建）
  GET  /bootstrap.ps1   虚拟机里的引导脚本
  POST /report          虚拟机回传的测试结果（落盘到 reports/*.jsonl）
  GET  /reports         看已收到的结果

用法：
  python -X utf8 deploy/vm/serve.py --port 8000
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import zipfile
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

VM_DIR = Path(__file__).resolve().parent
REPO = VM_DIR.parent.parent
STAGE = VM_DIR / "stage"
REPORTS = VM_DIR / "reports"
ZIP_PATH = STAGE / "repo.zip"
BOOTSTRAP = VM_DIR / "bootstrap.ps1"

EXCLUDE_DIRS = {".git", ".playwright-mcp", "data", "publish", "reports", "stage", "__pycache__", ".venv", "venv"}
EXCLUDE_SUFFIX = {".pyc", ".log"}


def build_zip() -> tuple[bool, str]:
    STAGE.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(ZIP_PATH, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in sorted(REPO.rglob("*")):
                rel = path.relative_to(REPO)
                if any(part in EXCLUDE_DIRS for part in rel.parts):
                    continue
                if path.is_dir() or path.suffix in EXCLUDE_SUFFIX:
                    continue
                zf.write(path, rel.as_posix())
        size = ZIP_PATH.stat().st_size / (1024 * 1024)
        return True, f"已打包 {ZIP_PATH}（{size:.1f} MB）"
    except Exception as exc:  # noqa: BLE001
        return False, f"打包失败：{exc}"


class Handler(BaseHTTPRequestHandler):
    server_version = "MyAgentVM/1.0"

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        print(f"[vm-serve] {self.address_string()} {fmt % args}", flush=True)

    def _send(self, code: int, body: bytes, ctype: str = "application/octet-stream") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        if path.startswith("/vm/"):
            name = path[len("/vm/"):]
            local = VM_DIR / name
            if not name or "/" in name or "\\" in name or ".." in name or not local.is_file():
                self._send(404, b"not found", "text/plain")
                return
            self._send(200, local.read_bytes(), "text/plain; charset=utf-8")
            return
        if path.startswith("/wheels/"):
            name = path[len("/wheels/"):]
            if not name or "/" in name or ".." in name:
                self._send(403, b"forbidden", "text/plain")
                return
            local = VM_DIR / "stage" / "wheels" / name
            if not local.exists():
                self._send(404, b"not found", "text/plain")
                return
            self._send(200, local.read_bytes(), "application/octet-stream")
            return
        if path == "/health":
            self._send(200, b'{"ok":true}', "application/json")
            return
        if path == "/repo.zip":
            if not ZIP_PATH.exists():
                ok, msg = build_zip()
                if not ok:
                    self._send(500, msg.encode("utf-8"), "text/plain; charset=utf-8")
                    return
            self._send(200, ZIP_PATH.read_bytes(), "application/zip")
            return
        if path == "/python.exe":
            local = Path(os.getenv("TEMP", "/tmp")) / "vm-bundle" / "python-3.11.9-amd64.exe"
            if not local.exists():
                self._send(404, b"python installer not bundled", "text/plain")
                return
            size = local.stat().st_size
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(size))
            self.end_headers()
            with local.open("rb") as fh:
                while True:
                    chunk = fh.read(1024 * 256)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
            return
        if path == "/live_model_list.json":
            local = STAGE / "live_model_list.json"
            if not local.exists():
                self._send(404, b"live_model_list.json not exported", "text/plain")
                return
            self._send(200, local.read_bytes(), "application/json; charset=utf-8")
            return
        if path == "/mock_onebot.py":
            local = VM_DIR / "mock_onebot.py"
            if not local.exists():
                self._send(404, b"not found", "text/plain")
                return
            self._send(200, local.read_bytes(), "text/plain; charset=utf-8")
            return
        if path == "/set_perm.py":
            local = VM_DIR / "set_perm.py"
            if not local.exists():
                self._send(404, b"not found", "text/plain")
                return
            self._send(200, local.read_bytes(), "text/plain; charset=utf-8")
            return
        if path == "/check_skill_state.py":
            local = VM_DIR / "check_skill_state.py"
            if not local.exists():
                self._send(404, b"not found", "text/plain")
                return
            self._send(200, local.read_bytes(), "text/plain; charset=utf-8")
            return
        if path == "/debug_clawhub.py":
            local = VM_DIR / "debug_clawhub.py"
            if not local.exists():
                self._send(404, b"not found", "text/plain")
                return
            self._send(200, local.read_bytes(), "text/plain; charset=utf-8")
            return
        if path == "/set_launcher_ov.py":
            local = VM_DIR / "set_launcher_ov.py"
            if not local.exists():
                self._send(404, b"not found", "text/plain")
                return
            self._send(200, local.read_bytes(), "text/plain; charset=utf-8")
            return
        if path == "/verify_fresh.ps1":
            local = VM_DIR / "verify_fresh.ps1"
            if not local.exists():
                self._send(404, b"not found", "text/plain")
                return
            self._send(200, local.read_bytes(), "text/plain; charset=utf-8")
            return
        if path == "/verify_memory.py":
            local = VM_DIR / "verify_memory.py"
            if not local.exists():
                self._send(404, b"not found", "text/plain")
                return
            self._send(200, local.read_bytes(), "text/plain; charset=utf-8")
            return
        if path == "/probe_openviking.py":
            local = VM_DIR / "probe_openviking.py"
            if not local.exists():
                self._send(404, b"not found", "text/plain")
                return
            self._send(200, local.read_bytes(), "text/plain; charset=utf-8")
            return
        if path == "/check_openviking_deps.py":
            local = VM_DIR / "check_openviking_deps.py"
            if not local.exists():
                self._send(404, b"not found", "text/plain")
                return
            self._send(200, local.read_bytes(), "text/plain; charset=utf-8")
            return
        if path == "/openviking_api_key.txt":
            local = STAGE / "openviking_api_key.txt"
            if not local.exists():
                self._send(404, b"key not exported", "text/plain")
                return
            self._send(200, local.read_bytes(), "text/plain; charset=ascii")
            return
        if path == "/verify_chat.py":
            local = VM_DIR / "verify_chat.py"
            if not local.exists():
                self._send(404, b"verify_chat.py not found", "text/plain")
                return
            self._send(200, local.read_bytes(), "text/plain; charset=utf-8")
            return
        if path == "/post_log.ps1":
            local = VM_DIR / "post_log.ps1"
            if not local.exists():
                self._send(404, b"not found", "text/plain")
                return
            self._send(200, local.read_bytes(), "text/plain; charset=utf-8")
            return
        if path == "/restart_mono.ps1":
            local = VM_DIR / "restart_mono.ps1"
            if not local.exists():
                self._send(404, b"not found", "text/plain")
                return
            self._send(200, local.read_bytes(), "text/plain; charset=utf-8")
            return
        if path == "/cap_probe.ps1":
            local = VM_DIR / "cap_probe.ps1"
            if not local.exists():
                self._send(404, b"not found", "text/plain")
                return
            self._send(200, local.read_bytes(), "text/plain; charset=utf-8")
            return
        if path == "/doc_walk.ps1":
            local = VM_DIR / "doc_walk.ps1"
            if not local.exists():
                self._send(404, b"not found", "text/plain")
                return
            self._send(200, local.read_bytes(), "text/plain; charset=utf-8")
            return
        if path == "/lock_test.ps1":
            local = VM_DIR / "lock_test.ps1"
            if not local.exists():
                self._send(404, b"not found", "text/plain")
                return
            self._send(200, local.read_bytes(), "text/plain; charset=utf-8")
            return
        if path == "/ov_final2.ps1":
            local = VM_DIR / "ov_final2.ps1"
            if not local.exists():
                self._send(404, b"not found", "text/plain")
                return
            self._send(200, local.read_bytes(), "text/plain; charset=utf-8")
            return
        if path == "/ov_id_probe.ps1":
            local = VM_DIR / "ov_id_probe.ps1"
            if not local.exists():
                self._send(404, b"not found", "text/plain")
                return
            self._send(200, local.read_bytes(), "text/plain; charset=utf-8")
            return
        if path == "/ov_api_probe.ps1":
            local = VM_DIR / "ov_api_probe.ps1"
            if not local.exists():
                self._send(404, b"not found", "text/plain")
                return
            self._send(200, local.read_bytes(), "text/plain; charset=utf-8")
            return
        if path == "/ov_final.ps1":
            local = VM_DIR / "ov_final.ps1"
            if not local.exists():
                self._send(404, b"not found", "text/plain")
                return
            self._send(200, local.read_bytes(), "text/plain; charset=utf-8")
            return
        if path == "/ov_stack.ps1":
            local = VM_DIR / "ov_stack.ps1"
            if not local.exists():
                self._send(404, b"not found", "text/plain")
                return
            self._send(200, local.read_bytes(), "text/plain; charset=utf-8")
            return
        if path == "/ov_retest.ps1":
            local = VM_DIR / "ov_retest.ps1"
            if not local.exists():
                self._send(404, b"not found", "text/plain")
                return
            self._send(200, local.read_bytes(), "text/plain; charset=utf-8")
            return
        if path == "/refresh_fresh.ps1":
            local = VM_DIR / "refresh_fresh.ps1"
            if not local.exists():
                self._send(404, b"not found", "text/plain")
                return
            self._send(200, local.read_bytes(), "text/plain; charset=utf-8")
            return
        if path == "/fresh_deploy.ps1":
            local = VM_DIR / "fresh_deploy.ps1"
            if not local.exists():
                self._send(404, b"not found", "text/plain")
                return
            self._send(200, local.read_bytes(), "text/plain; charset=utf-8")
            return
        if path == "/clean_restart.ps1":
            local = VM_DIR / "clean_restart.ps1"
            if not local.exists():
                self._send(404, b"not found", "text/plain")
                return
            self._send(200, local.read_bytes(), "text/plain; charset=utf-8")
            return
        if path == "/del_ov_key.ps1":
            local = VM_DIR / "del_ov_key.ps1"
            if not local.exists():
                self._send(404, b"not found", "text/plain")
                return
            self._send(200, local.read_bytes(), "text/plain; charset=utf-8")
            return
        if path == "/set_ov_key.ps1":
            local = VM_DIR / "set_ov_key.ps1"
            if not local.exists():
                self._send(404, b"not found", "text/plain")
                return
            self._send(200, local.read_bytes(), "text/plain; charset=utf-8")
            return
        if path == "/ovreset2.ps1":
            local = VM_DIR / "ovreset2.ps1"
            if not local.exists():
                self._send(404, b"not found", "text/plain")
                return
            self._send(200, local.read_bytes(), "text/plain; charset=utf-8")
            return
        if path == "/reset_ov.ps1":
            local = VM_DIR / "reset_ov.ps1"
            if not local.exists():
                self._send(404, b"not found", "text/plain")
                return
            self._send(200, local.read_bytes(), "text/plain; charset=utf-8")
            return
        if path == "/ovinstall.ps1":
            local = VM_DIR / "ovinstall.ps1"
            if not local.exists():
                self._send(404, b"not found", "text/plain")
                return
            self._send(200, local.read_bytes(), "text/plain; charset=utf-8")
            return
        if path == "/ovrun.ps1":
            local = VM_DIR / "ovrun.ps1"
            if not local.exists():
                self._send(404, b"not found", "text/plain")
                return
            self._send(200, local.read_bytes(), "text/plain; charset=utf-8")
            return
        if path == "/ovdeps.ps1":
            local = VM_DIR / "ovdeps.ps1"
            if not local.exists():
                self._send(404, b"not found", "text/plain")
                return
            self._send(200, local.read_bytes(), "text/plain; charset=utf-8")
            return
        if path == "/ovfeas.ps1":
            local = VM_DIR / "ovfeas.ps1"
            if not local.exists():
                self._send(404, b"not found", "text/plain")
                return
            self._send(200, local.read_bytes(), "text/plain; charset=utf-8")
            return
        if path == "/setup_ov.ps1":
            local = VM_DIR / "setup_ov.ps1"
            if not local.exists():
                self._send(404, b"not found", "text/plain")
                return
            self._send(200, local.read_bytes(), "text/plain; charset=utf-8")
            return
        if path == "/setup_satori.ps1":
            local = VM_DIR / "setup_satori.ps1"
            if not local.exists():
                self._send(404, b"not found", "text/plain")
                return
            self._send(200, local.read_bytes(), "text/plain; charset=utf-8")
            return
        if path == "/setup_onebot.ps1":
            local = VM_DIR / "setup_onebot.ps1"
            if not local.exists():
                self._send(404, b"not found", "text/plain")
                return
            self._send(200, local.read_bytes(), "text/plain; charset=utf-8")
            return
        if path == "/verify_mono.ps1":
            local = VM_DIR / "verify_mono.ps1"
            if not local.exists():
                self._send(404, b"verify_mono.ps1 not found", "text/plain")
                return
            self._send(200, local.read_bytes(), "text/plain; charset=utf-8")
            return
        if path == "/bootstrap.ps1":
            if not BOOTSTRAP.exists():
                self._send(404, b"bootstrap.ps1 not found", "text/plain")
                return
            self._send(200, BOOTSTRAP.read_bytes(), "text/plain; charset=utf-8")
            return
        if path == "/reports":
            files = sorted(REPORTS.glob("*.jsonl"))
            body = "".join(f.read_text(encoding="utf-8") for f in files[-5:])
            self._send(200, body.encode("utf-8"), "text/plain; charset=utf-8")
            return
        self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:  # noqa: N802
        if self.path.split("?")[0] != "/report":
            self._send(404, b"not found", "text/plain")
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        try:
            data = json.loads(raw)
        except Exception:  # noqa: BLE001
            data = {"raw": raw}
        data["received_at"] = datetime.now().isoformat(timespec="seconds")
        REPORTS.mkdir(parents=True, exist_ok=True)
        stamped = REPORTS / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}.jsonl"
        with stamped.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(data, ensure_ascii=False) + "\n")
        print(f"[vm-serve] 收到回报：{json.dumps(data, ensure_ascii=False)[:200]}", flush=True)
        self._send(200, b'{"ok":true}', "application/json")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="虚拟机自测用的宿主 HTTP 服务")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--rebuild", action="store_true", help="启动前强制重建 repo.zip")
    args = parser.parse_args(argv[1:])

    if args.rebuild or not ZIP_PATH.exists():
        ok, msg = build_zip()
        print(msg, flush=True)
        if not ok:
            return 1

    server = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    print(f"[vm-serve] 监听 0.0.0.0:{args.port}（虚拟机里用 http://10.0.2.2:{args.port}）", flush=True)
    print(f"[vm-serve] 项目包：{ZIP_PATH}（{ZIP_PATH.stat().st_size / 1024 / 1024:.1f} MB）", flush=True)
    print(f"[vm-serve] 回传目录：{REPORTS}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("[vm-serve] 停止", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

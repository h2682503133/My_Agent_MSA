"""单体图床自检：真起 assets.py（回环随机端口），验证 /assets 静态与安全边界。"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "deploy"))

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


def main() -> int:
    tmp = Path(tempfile.mkdtemp())
    images = tmp / "assets" / "images"
    images.mkdir(parents=True)
    (images / "cat.png").write_bytes(b"\x89PNG\r\n\x1a\n-fake")
    (images / "note.json").write_text('{"k":1}', encoding="utf-8")
    (tmp / "secret.txt").write_text("SECRET", encoding="utf-8")

    port = free_port()
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "deploy" / "launcher" / "assets.py"),
         "--port", str(port), "--dir", str(images)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        check("图床端口就绪", wait_port(port), str(port))
        base = f"http://127.0.0.1:{port}"

        with urllib.request.urlopen(base + "/health", timeout=5) as resp:
            payload = json.loads(resp.read())
            check("健康检查", resp.status == 200 and payload["ok"] is True, str(payload))

        with urllib.request.urlopen(base + "/assets/cat.png", timeout=5) as resp:
            body = resp.read()
            check("图片可访问", resp.status == 200 and body.startswith(b"\x89PNG"))
            check("图片 MIME", resp.headers.get("Content-Type") == "image/png", resp.headers.get("Content-Type"))

        with urllib.request.urlopen(base + "/assets/note.json", timeout=5) as resp:
            check("json 可访问", resp.status == 200 and json.loads(resp.read())["k"] == 1)

        for path, label in (("/assets/missing.png", "不存在的图片"),
                            ("/assets/../../secret.txt", "路径穿越"),
                            ("/etc/passwd", "非 /assets 路径")):
            try:
                urllib.request.urlopen(base + path, timeout=5)
                check(f"{label} 应 404", False, path)
            except urllib.error.HTTPError as exc:
                check(f"{label} 返回 {exc.code}", exc.code == 404, str(exc.code))
            except Exception as exc:  # noqa: BLE001
                check(f"{label} 被拒", True, str(exc)[:40])
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    print()
    if FAILS:
        print(f"FAILED {len(FAILS)}: {FAILS}")
        return 1
    print(f"ALL OK — {CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

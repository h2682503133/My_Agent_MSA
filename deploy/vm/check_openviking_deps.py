"""查出 openviking 真正缺哪些运行期依赖（按 import 链逐个试）。"""

import importlib
import importlib.metadata as md
import json
import urllib.request

HOST_BASE = "http://10.244.37.37:8000"
CANDIDATES = [
    "loguru", "orjson", "ujson", "aiofiles", "tiktoken", "numpy", "pandas",
    "jinja2", "python-multipart", "starlette", "fastapi", "uvicorn", "yaml",
    "toml", "tomli", "tenacity", "backoff", "sse-starlette", "websockets",
    "psutil", "colorama", "rich", "typer", "click", "httpx", "aiohttp",
]

report = {}

try:
    report["requires"] = list(md.requires("openviking") or [])
except Exception as exc:  # noqa: BLE001
    report["requires"] = [f"读取失败: {exc}"]

missing = []
for name in CANDIDATES:
    try:
        importlib.import_module(name.replace("-", "_"))
    except Exception:  # noqa: BLE001
        missing.append(name)
report["missing_candidates"] = missing

# 真正 import openviking 时缺哪些模块（逐个补齐，最多试 8 轮）
report["import_attempts"] = []
for _ in range(8):
    try:
        importlib.import_module("openviking")
        report["import_attempts"].append("OK")
        break
    except ModuleNotFoundError as exc:
        mod = getattr(exc, "name", "") or str(exc)
        report["import_attempts"].append(f"缺 {mod}")
        pkg = mod.split(".")[0]
        try:
            importlib.import_module(pkg)
        except Exception:
            pass
        break
    except Exception as exc:  # noqa: BLE001
        report["import_attempts"].append(f"其它错误: {exc}")
        break

print(json.dumps(report, ensure_ascii=False, indent=2))

try:
    payload = json.dumps({"stage": "openviking-deps", "ok": True,
                          "detail": json.dumps(report, ensure_ascii=False),
                          "host": "vm"}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(f"{HOST_BASE}/report", data=payload, method="POST",
                                 headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=10).read()
except Exception as exc:  # noqa: BLE001
    print("回传失败:", exc)

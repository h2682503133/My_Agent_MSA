import os, sys
sys.path.insert(0, "/app")
from pathlib import Path
from app import tool_registry, tool_handlers, skill_handlers  # noqa
from app.tool_registry import dispatch
print("providers:", os.getenv("TOOL_PROVIDERS"))
print("catalog:", os.getenv("TOOL_CATALOG_PATH"))
print("已注册:", len(tool_registry.registered()), "| 别名:", len(tool_registry.alias_map()))
root = Path("/app/workspace")
print("dir-list:", dispatch("dir-list", [], {}, root, 30)[:120].replace("\n", " | "))
print("别名 view-image ->", tool_registry.canonical("view-image"))
print("windows-file-copy 存在:", "windows-file-copy" in tool_registry.registered())

"""统一存储前缀（MYAGENT_STORAGE_ROOT）解析自检。离线，纯函数。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

SERVICE = Path(__file__).resolve().parent.parent
for candidate in (SERVICE, SERVICE / "app"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

import storage_paths as sp  # noqa: E402

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


def norm(value) -> str:
    return str(value).replace("\\", "/").rstrip("/") or "/"


def main() -> int:
    saved = {k: os.environ.get(k) for k in
             ("MYAGENT_STORAGE_ROOT", "WORKSPACE_DIR", "WORKSPACE_USERS_BASE", "ASSETS_DIR")}
    for key in saved:
        os.environ.pop(key, None)
    try:
        print("没有任何配置 → 容器默认")
        check("users_base 默认 /app/workspace/users",
              norm(sp.users_base()) == "/app/workspace/users", norm(sp.users_base()))
        check("resolve 用 default",
              norm(sp.resolve("ASSETS_DIR", "assets", default="/app/assets")) == "/app/assets")

        print("只给 WORKSPACE_DIR（旧配置兼容）")
        os.environ["WORKSPACE_DIR"] = "/srv/ws"
        check("users 从 WORKSPACE_DIR 推导", norm(sp.users_base()) == "/srv/ws/users", norm(sp.users_base()))

        print("只给 MYAGENT_STORAGE_ROOT（推荐）")
        os.environ.pop("WORKSPACE_DIR", None)
        os.environ["MYAGENT_STORAGE_ROOT"] = "/data/agent"
        check("workspace", norm(sp.resolve("WORKSPACE_DIR", "workspace")) == "/data/agent/workspace")
        check("users", norm(sp.users_base()) == "/data/agent/workspace/users", norm(sp.users_base()))
        check("assets", norm(sp.resolve("ASSETS_DIR", "assets")) == "/data/agent/assets")
        check("assets/images", norm(sp.resolve("IMAGE_ASSET_DIR", "assets", "images")) == "/data/agent/assets/images")
        check("字符串版", sp.resolve_str("IMAGE_ASSET_DIR", "assets", "images").replace("\\", "/")
              == "/data/agent/assets/images")

        print("显式变量优先于前缀（k8s 清单照旧生效）")
        os.environ["ASSETS_DIR"] = "/explicit/assets"
        check("显式优先", norm(sp.resolve("ASSETS_DIR", "assets")) == "/explicit/assets")
        os.environ["WORKSPACE_USERS_BASE"] = "/explicit/ws/users"
        check("users 显式优先", norm(sp.users_base()) == "/explicit/ws/users")
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    print()
    if FAILS:
        print(f"FAILED {len(FAILS)}: {FAILS}")
        return 1
    print(f"ALL OK — {CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

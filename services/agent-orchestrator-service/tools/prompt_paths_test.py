"""提示词路径占位符自检（离线，纯函数）。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

SERVICE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVICE))

from app import prompt_paths  # noqa: E402

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


def norm(value: str) -> str:
    return str(value).replace("\\", "/")


def main() -> int:
    saved = {k: os.environ.get(k) for k in ("WORKSPACE_DIR", "IMAGE_ASSET_DIR")}
    try:
        os.environ["WORKSPACE_DIR"] = "/data/agent/workspace"
        os.environ.pop("IMAGE_ASSET_DIR", None)

        text = "工作空间 `{{WORKSPACE_DIR}}`，用户目录 `{{WORKSPACE_USERS_DIR}}/<user_id>`。"
        out = prompt_paths.render(text)
        check("WORKSPACE_DIR 被替换", "/data/agent/workspace" in norm(out), out)
        check("WORKSPACE_USERS_DIR 被替换", "/data/agent/workspace/users/<user_id>" in norm(out), out)
        check("没有残留 {{", "{{" not in out, out)

        check("ASSETS_DIR 默认在工作空间同级",
              norm(prompt_paths.render("{{ASSETS_DIR}}")) == "/data/agent/assets/images",
              norm(prompt_paths.render("{{ASSETS_DIR}}")))

        os.environ["IMAGE_ASSET_DIR"] = "/data/agent/assets/images"
        check("ASSETS_DIR 优先读 IMAGE_ASSET_DIR",
              norm(prompt_paths.render("{{ASSETS_DIR}}")) == "/data/agent/assets/images")

        check("未知占位符原样保留",
              prompt_paths.render("{{UNKNOWN_TOKEN}}") == "{{UNKNOWN_TOKEN}}")
        check("无占位符原样返回", prompt_paths.render("普通文本") == "普通文本")
        check("空串安全", prompt_paths.render("") == "")

        # 真实模板：渲染后不应再有 /app/workspace、/srv/nfs
        root = SERVICE.parent.parent / "config" / "orchestrator" / "system_prompt"
        offenders = []
        for path in root.rglob("*"):
            if path.is_file() and path.suffix in {".md", ".json"}:
                rendered = prompt_paths.render(path.read_text(encoding="utf-8"))
                if "/app/workspace" in rendered or "/srv/nfs" in rendered:
                    offenders.append(str(path.relative_to(root)))
        check("模板渲染后没有写死路径", not offenders, str(offenders))
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

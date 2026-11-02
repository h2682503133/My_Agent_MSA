"""提示词里的路径占位符。

为什么需要：提示词文件（GLOBAL_SETTING.md / TOOL.md / SOUL.md / TOOL.json）是**模板**，
会被复制到各种环境使用。写死 `/app/workspace/users/<user_id>` 在容器里成立，
但单机（Windows）、compose、其他集群挂载路径都不同。

约定：提示词里写占位符，orchestrator 读文件时替换成**当前环境的真实路径**：

| 占位符 | 替换为 |
|---|---|
| `{{WORKSPACE_DIR}}` | 工具运行服务的基础工作空间（config.WORKSPACE_DIR） |
| `{{WORKSPACE_USERS_DIR}}` | 用户工作目录的父目录（`<WORKSPACE_DIR>/users`） |
| `{{ASSETS_DIR}}` | 图床/素材目录（`IMAGE_ASSET_DIR`，未配置则 `<工作空间>/assets`） |

未识别的占位符原样保留（不会把提示词改坏），换行/缩进保持不变。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

TOKEN_RE = re.compile(r"\{\{([A-Z_]+)\}\}")


def _workspace_dir() -> Path:
    try:
        from app import config  # 延迟导入，避免测试时拉起整套配置

        return Path(getattr(config, "WORKSPACE_DIR", "/app/workspace"))
    except Exception:  # noqa: BLE001
        return Path(os.getenv("WORKSPACE_DIR", "/app/workspace"))


def _assets_dir() -> Path:
    raw = (os.getenv("IMAGE_ASSET_DIR") or "").strip()
    if raw:
        return Path(raw)
    return _workspace_dir().parent / "assets" / "images"


def values() -> dict[str, str]:
    workspace = _workspace_dir()
    return {
        "WORKSPACE_DIR": str(workspace),
        "WORKSPACE_USERS_DIR": str(workspace / "users"),
        "ASSETS_DIR": str(_assets_dir()),
    }


def render(text: str) -> str:
    """把占位符替换成真实路径；未识别的原样保留。"""
    if not text or "{{" not in text:
        return text
    table = values()

    def _sub(match: re.Match) -> str:
        token = match.group(1)
        return table.get(token, match.group(0))

    return TOKEN_RE.sub(_sub, text)

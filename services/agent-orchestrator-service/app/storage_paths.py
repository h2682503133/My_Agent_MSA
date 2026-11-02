"""统一的存储前缀（STORAGE_ROOT）。

问题背景：各服务各自读 `WORKSPACE_DIR` / `WORKSPACE_USERS_BASE` / `ASSETS_DIR` / `IMAGE_ASSET_DIR`
等一串环境变量，默认值又不一致（`/app/workspace` vs `BASE_DIR/workspace`），
单机部署时只要漏注入一个，就会出现"上传到 A 目录、工具从 B 目录读"这类空目录问题。

约定（优先级从高到低）：
1. 该路径自己的环境变量（如 `WORKSPACE_DIR`）—— 兼容 k8s 清单里显式指定；
2. `MYAGENT_STORAGE_ROOT`（存储前缀）下按相对结构推导；
3. 服务自己的默认值（容器内 `/app/...`）。

单机/单体只需注入一个 `MYAGENT_STORAGE_ROOT=<数据目录>`，所有路径自动对齐。
"""

from __future__ import annotations

import os
from pathlib import Path

STORAGE_ROOT_ENV = "MYAGENT_STORAGE_ROOT"


def storage_root() -> Path | None:
    """返回存储前缀（未配置时返回 None）。"""
    raw = (os.getenv(STORAGE_ROOT_ENV) or "").strip()
    return Path(raw) if raw else None


def resolve(env_name: str, *relative_parts: str, default: str = "") -> Path:
    """按优先级解析一个路径。

    env_name        该路径专属的环境变量名，例如 "WORKSPACE_DIR"
    relative_parts  相对存储前缀的路径，例如 ("workspace",)
    default         都没配置时的兜底（一般是容器内的 /app/...）
    """
    explicit = (os.getenv(env_name) or "").strip()
    if explicit:
        return Path(explicit)
    root = storage_root()
    if root is not None:
        return root.joinpath(*relative_parts)
    return Path(default) if default else Path(*relative_parts)


def resolve_str(env_name: str, *relative_parts: str, default: str = "") -> str:
    return str(resolve(env_name, *relative_parts, default=default))


def users_base(env_name: str = "WORKSPACE_USERS_BASE", default: str = "/app/workspace/users") -> Path:
    """用户工作区根（`<root>/workspace/users`）。

    兼容旧配置：只给了 `WORKSPACE_DIR` 时，取它的 `users` 子目录。
    """
    explicit = (os.getenv(env_name) or "").strip()
    if explicit:
        return Path(explicit)
    workspace = (os.getenv("WORKSPACE_DIR") or "").strip()
    if workspace:
        return Path(workspace) / "users"
    root = storage_root()
    if root is not None:
        return root / "workspace" / "users"
    return Path(default)

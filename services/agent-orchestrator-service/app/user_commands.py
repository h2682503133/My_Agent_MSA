"""会话状态（默认智能体 / 模式）的服务端操作。

命令的**解析**在 task-scheduler-service（`app/state_commands.py`，和 `/stop` 同层），
这里只负责校验与写内存状态：

    /agent            查看当前智能体与可用列表
    /agent talker     切换智能体（模式同时重置为 default）
    /mode             查看当前模式与可用列表
    /mode eco         切换模式（不动 agent_id → 会话上下文共用）
    /mode default     回到默认模式
"""

from __future__ import annotations

import json
from pathlib import Path

def available_agents(config_path: Path, user_id: str) -> list[str]:
    """从 agent_list.json 列出可用智能体（default 全量 + 该用户覆盖的并集）。"""
    try:
        data = json.loads(Path(config_path).read_text(encoding="utf-8"))
    except Exception:
        return []
    names: list[str] = []

    def _collect(block) -> None:
        if isinstance(block, dict):
            for key in block:
                text = str(key or "").strip()
                if text and text not in names:
                    names.append(text)

    _collect(data.get("default"))
    users = data.get("users")
    if isinstance(users, dict):
        _collect(users.get(str(user_id or "")))
    return names


def agent_exists(config_path: Path, user_id: str, agent_id: str) -> bool:
    """该智能体是否可加载（default 或用户覆盖里定义过）。"""
    name = str(agent_id or "").strip()
    if not name:
        return False
    return name in available_agents(config_path, user_id)

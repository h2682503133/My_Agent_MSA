"""openviking-context-service 不可用时的本地降级上下文。

每个 (user_id, session_id, agent_id) 维护最近 MAX_TURNS 个对话回合，
用户消息 + 智能体回复算 1 条。以 JSON 文件存储于 config 卷（PROCESS_DIR 同级），
重启后仍可延续（与 OpenViking 的会话记忆语义保持一致）。
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from app import config

MAX_TURNS = 4


def _store_dir() -> Path:
    base = Path(getattr(config, "PROCESS_DIR", "/app/config/process"))
    d = base.parent / "local_context"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _safe(part: str) -> str:
    """清洗标识符，防止路径穿越；保留字母数字与常见符号。"""
    cleaned = re.sub(r"[^0-9A-Za-z_\-.@]", "_", part or "default")
    return cleaned[:64] or "default"


def _file_for(user_id: str, session_id: str, agent_id: str) -> Path:
    return _store_dir() / f"{_safe(user_id)}__{_safe(session_id)}__{_safe(agent_id)}.json"


def search(user_id: str, session_id: str, agent_id: str = "main") -> list[dict]:
    """返回最近 MAX_TURNS 个回合的 [{role, content}] 消息列表；无历史时返回空列表。"""
    path = _file_for(user_id, session_id, agent_id)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        turns = data.get("turns", [])[-MAX_TURNS:]
        messages: list[dict] = []
        for t in turns:
            if not isinstance(t, dict):
                continue
            if t.get("user"):
                messages.append({"role": "user", "content": str(t["user"])})
            if t.get("assistant"):
                messages.append({"role": "assistant", "content": str(t["assistant"])})
        return messages
    except Exception:
        return []


def append(
    user_id: str,
    session_id: str,
    agent_id: str,
    user_message: str,
    assistant_message: str,
) -> bool:
    """追加一个回合（用户+智能体回复算 1 条），只保留最近 MAX_TURNS 条。"""
    path = _file_for(user_id, session_id, agent_id)
    turns: list[dict] = []
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            turns = data.get("turns", [])
        except Exception:
            turns = []

    turns.append({
        "user": user_message or "",
        "assistant": assistant_message or "",
        "ts": datetime.now().isoformat(),
    })
    turns = turns[-MAX_TURNS:]

    try:
        path.write_text(json.dumps({"turns": turns}, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    except Exception:
        return False

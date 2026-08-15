"""PROCESS 长期事件记录存储读写（内置工具）。

存储：<PROCESS_DIR>/<user_id>/<agent_id>.json
结构：{"turn": N, "items": [{"title": "...", "content": "..."}]}
      turn 仅轮次模式由 orchestrator 每次读取时递增维护，此处读写保持原样。

内置工具：
- process-write|index|title|content   index=-1 追加到末尾；1..N 覆写对应条目
- process-remove|index                按 1 起始删除对应条目
- process-init                        重置为初始状态（清空条目、轮次归零）
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path


def _safe_segment(value: str, default: str = "default") -> str:
    """user_id / agent_id 转安全目录名，避免路径逃逸。"""
    raw = str(value or default).strip() or default
    safe = re.sub(r"[^0-9A-Za-z_.@-]+", "_", raw).strip("._") or default
    return "default" if safe in {".", ".."} else safe


def _process_path(process_dir: str, user_id: str, agent_id: str) -> Path:
    return Path(process_dir) / _safe_segment(user_id) / f"{_safe_segment(agent_id)}.json"


def _load(path: Path) -> dict:
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {"items": []}


def _save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _parse_index(index: str) -> int | None:
    try:
        return int(str(index or "").strip())
    except (TypeError, ValueError):
        return None


def write(process_dir: str, user_id: str, agent_id: str, index: str, title: str, content: str) -> str:
    path = _process_path(process_dir, user_id, agent_id)
    data = _load(path)
    items = data.get("items")
    if not isinstance(items, list):
        items = []
        data["items"] = items

    idx = _parse_index(index)
    if idx is None:
        return "错误：index 必须是整数（-1 表示追加到末尾，1..N 表示覆写对应条目）"

    title = str(title or "").strip()
    content = str(content or "").strip()
    entry = {"title": title, "content": content}

    if idx == -1:
        items.append(entry)
        _save(path, data)
        return f"已追加第 {len(items)} 条：{title or '(无标题)'}"

    if 1 <= idx <= len(items):
        items[idx - 1] = entry
        _save(path, data)
        return f"已覆写第 {idx} 条：{title or '(无标题)'}"

    if idx == len(items) + 1:
        items.append(entry)
        _save(path, data)
        return f"已追加第 {len(items)} 条：{title or '(无标题)'}"

    return f"错误：index={idx} 越界（当前共 {len(items)} 条，-1 或 {len(items) + 1} 表示末尾追加，1..{len(items)} 表示覆写）"


def remove(process_dir: str, user_id: str, agent_id: str, index: str) -> str:
    path = _process_path(process_dir, user_id, agent_id)
    data = _load(path)
    items = data.get("items")
    if not isinstance(items, list):
        items = []
        data["items"] = items

    idx = _parse_index(index)
    if idx is None:
        return "错误：index 必须是整数（1..N）"
    if not (1 <= idx <= len(items)):
        return f"错误：index={idx} 越界（当前共 {len(items)} 条）"

    removed = items.pop(idx - 1)
    if isinstance(removed, dict):
        title = str(removed.get("title") or "").strip()
    else:
        title = str(removed)
    _save(path, data)
    return f"已删除第 {idx} 条：{title or '(无标题)'}（剩余 {len(items)} 条）"


def init(process_dir: str, user_id: str, agent_id: str) -> str:
    """重置整个 PROCESS：清空条目、轮次归零。"""
    path = _process_path(process_dir, user_id, agent_id)
    _save(path, {"turn": 0, "items": []})
    return "已初始化 PROCESS（条目已清空，轮次归零）"

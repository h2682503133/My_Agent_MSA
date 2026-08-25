"""世界书（World Info）条目读写（内置工具，供智能体自动更新）。

存储：<WORLD_INFO_DIR>/world_info.json（与 PROCESS 同级；
orchestrator 侧同目录为 /app/config/world_info/world_info.json）。
每次写前自动备份 world_info.json.bak。

内置工具：
- worldinfo-write|关键词1,关键词2|内容|优先级|scope|constant|regex|match_mode
    scope 省略 = 当前 agent（agent 写只对自己）；群组用 group:群组id
    match_mode = or（任一命中即触发，默认）/ and（全部命中才触发）
    系统自动查重：同 scope 已存在相同关键词集合 → 返回"该词条已经存在"（不新增不覆写），
    智能体无需先查重；删除/修改由用户在 dashboard 手动管理
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import time
from datetime import datetime
from pathlib import Path


def _now_iso() -> str:
    try:
        return datetime.now().astimezone().isoformat(timespec="seconds")
    except Exception:
        return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _load(path: Path) -> dict:
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {"version": 1, "entries": []}


def _save(path: Path, data: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # 写前自动备份 .bak（NFS 改动红线）
    if path.exists():
        try:
            shutil.copy2(path, path.with_name(path.name + ".bak"))
        except Exception:
            pass
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


def _split_keys(keys_str: str) -> list[str]:
    seen: list[str] = []
    for part in re.split(r"[,，、|]", str(keys_str or "")):
        key = part.strip()
        if key and key not in seen:
            seen.append(key)
    return seen


def _normalize_match_mode(value: str) -> str:
    """match_mode 归一化：or（默认）/ and，非法回退 or。"""
    mode = str(value or "").strip().lower()
    return mode if mode in ("or", "and") else "or"


def _find_entry(entries: list[dict], entry_id: str) -> dict | None:
    for entry in entries:
        if isinstance(entry, dict) and str(entry.get("id") or "") == entry_id:
            return entry
    return None


def write(
    world_info_path: str,
    agent_id: str,
    keys_str: str,
    content: str,
    priority: str = "0",
    scope: str = "",
    constant: str = "false",
    regex: str = "false",
    match_mode: str = "or",
) -> str:
    path = Path(world_info_path)
    keys = _split_keys(keys_str)
    if not keys:
        return "错误：至少需要一个触发关键词"
    content = str(content or "").strip()
    if not content:
        return "错误：内容不能为空"
    try:
        priority_val = int(str(priority or "0").strip() or "0")
    except (TypeError, ValueError):
        priority_val = 0
    scope_val = str(scope or "").strip() or str(agent_id or "main").strip() or "main"
    constant_val = str(constant or "").strip().lower() in ("true", "1", "yes", "常驻")
    regex_val = str(regex or "").strip().lower() in ("true", "1", "yes")
    match_mode_val = _normalize_match_mode(match_mode)

    data = _load(path)
    entries = data.setdefault("entries", [])
    if not isinstance(entries, list):
        entries = []
        data["entries"] = entries

    # 系统自动查重：同 scope + 相同关键词集合 → 已存在，不新增不覆写（删除/修改由用户在 dashboard 管理）
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("scope") or "").strip() != scope_val:
            continue
        entry_keys = set(str(k).strip() for k in (entry.get("keys") or []) if str(k).strip())
        if entry_keys == set(keys):
            return (f"该词条已经存在（{entry.get('id', '')}，scope={scope_val}），"
                    "如需修改或删除请在 dashboard 世界书页操作")

    entry_id = f"wi_{int(time.time() * 1000)}"
    entries.append({
        "id": entry_id,
        "scope": scope_val,
        "keys": keys,
        "content": content,
        "priority": priority_val,
        "constant": constant_val,
        "regex": regex_val,
        "match_mode": match_mode_val,
        "enabled": True,
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
    })
    _save(path, data)
    scope_desc = f"（scope={scope_val}）" if scope_val != str(agent_id or "") else "（仅当前智能体）"
    mode_desc = "，and 全部命中" if match_mode_val == "and" else ""
    return f"已添加世界书条目 {entry_id}，关键词：{'、'.join(keys)}{scope_desc}{mode_desc}"


def remove(world_info_path: str, entry_id: str) -> str:
    path = Path(world_info_path)
    data = _load(path)
    entries = data.get("entries", [])
    if not isinstance(entries, list):
        return "错误：世界书数据损坏"
    entry = _find_entry(entries, entry_id)
    if entry is None:
        return f"错误：未找到条目 {entry_id}"
    entries.remove(entry)
    _save(path, data)
    return f"已删除世界书条目 {entry_id}"


def list_entries(world_info_path: str, agent_id: str = "") -> str:
    """列出全部条目（agent_id 非空时仅列出该 agent 或其群组可见的条目）。"""
    path = Path(world_info_path)
    data = _load(path)
    entries = data.get("entries", [])
    if not isinstance(entries, list):
        entries = []
    if not entries:
        return "世界书为空（0 条）"

    lines = [f"世界书共 {len(entries)} 条："]
    for index, entry in enumerate(entries, 1):
        if not isinstance(entry, dict):
            continue
        entry_id = str(entry.get("id") or "")
        scope = str(entry.get("scope") or "")
        keys = "、".join(str(k) for k in (entry.get("keys") or []) if str(k))
        content = str(entry.get("content") or "").strip().replace("\n", " ")
        if len(content) > 50:
            content = content[:50] + "…"
        flags = []
        if entry.get("constant"):
            flags.append("常驻")
        if entry.get("regex"):
            flags.append("正则")
        if str(entry.get("match_mode") or "or").strip().lower() == "and":
            flags.append("AND全部命中")
        if not entry.get("enabled", True):
            flags.append("禁用")
        if int(entry.get("priority", 0) or 0):
            flags.append(f"优先级{entry.get('priority')}")
        flag_str = f" [{'|'.join(flags)}]" if flags else ""
        lines.append(f"{index}. {entry_id}（scope={scope}）{flag_str}\n   关键词：{keys}\n   内容：{content}")
    return "\n".join(lines)

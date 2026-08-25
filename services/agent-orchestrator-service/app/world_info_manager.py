"""世界书（World Info）条目管理（orchestrator 内置命令用）。

存储：<WORLD_INFO_DIR>/world_info.json（与 PROCESS 同级，orchestrator 挂载可写）。
每次写前自动备份 world_info.json.bak（NFS 改动红线惯例）。

条目字段：
- id         wi_<毫秒时间戳>（自动生成）
- scope      agent_id（默认，仅该智能体可见）或 group:群组id（群组内所有智能体可见）
             无全局作用域：空 scope 条目不参与匹配（手写时须显式填 scope）
- keys       触发关键词列表（子串匹配，大小写不敏感）
- content    注入内容
- priority   优先级（数值大者先注入）
- constant   常驻条目（不靠触发）
- regex      keys 按正则 re.search 匹配
- match_mode or（任一关键词命中即触发，默认）/ and（全部关键词命中才触发）
- enabled    开关
"""

from __future__ import annotations

import json
import re
import shutil
import time
from datetime import datetime
from pathlib import Path

from app import config
from app.logger import debug_log


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
        except Exception as exc:
            debug_log(f"world_info: 读取 {path} 失败: {exc}")
    return {"version": 1, "entries": []}


def _save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # 写前自动备份 .bak（NFS 改动红线）
    if path.exists():
        try:
            shutil.copy2(path, path.with_name(path.name + ".bak"))
        except Exception as exc:
            debug_log(f"world_info: 备份失败: {exc}")
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _entries() -> list[dict]:
    data = _load(config.WORLD_INFO_PATH)
    entries = data.get("entries", [])
    if not isinstance(entries, list):
        return []
    return entries


def _split_keys(keys_str: str) -> list[str]:
    """关键词按逗号/顿号/竖线分隔，去空去重。"""
    seen: list[str] = []
    for part in re.split(r"[,，、|]", str(keys_str or "")):
        key = part.strip()
        if key and key not in seen:
            seen.append(key)
    return seen


def _entry_preview(content: str, limit: int = 50) -> str:
    content = str(content or "").strip().replace("\n", " ")
    return content if len(content) <= limit else content[:limit] + "…"


def _normalize_match_mode(value: str) -> str:
    """match_mode 归一化：or（默认）/ and，非法回退 or。"""
    mode = str(value or "").strip().lower()
    return mode if mode in ("or", "and") else "or"


def add(
    agent_id: str,
    keys_str: str,
    content: str,
    priority: str = "0",
    scope: str = "",
    constant: str = "false",
    regex: str = "false",
    match_mode: str = "or",
) -> str:
    """添加条目。scope 省略时默认当前 agent（agent 写只对自己）。

    系统自动查重：同 scope 且关键词集合完全相同的条目 → 返回"该词条已经存在"，
    不新增不覆写（删除/修改由用户在 dashboard 手动管理）。
    """
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
    scope_val = str(scope or "").strip() or agent_id
    constant_val = str(constant or "").strip().lower() in ("true", "1", "yes", "常驻")
    regex_val = str(regex or "").strip().lower() in ("true", "1", "yes")
    match_mode_val = _normalize_match_mode(match_mode)

    path = config.WORLD_INFO_PATH
    data = _load(path)
    entries = data.setdefault("entries", [])
    if not isinstance(entries, list):
        entries = []
        data["entries"] = entries

    # 系统自动查重：同 scope + 相同关键词集合 → 已存在，不新增不覆写
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
    scope_desc = f"（scope={scope_val}）" if scope_val != agent_id else "（仅当前智能体）"
    mode_desc = "，and 全部命中" if match_mode_val == "and" else ""
    return f"已添加世界书条目 {entry_id}，关键词：{'、'.join(keys)}{scope_desc}{mode_desc}"


def list_all(agent_id: str = "") -> str:
    """列出全部条目（可选按 agent 过滤）。"""
    entries = _entries()
    if not entries:
        return "世界书为空（0 条）"
    lines = [f"世界书共 {len(entries)} 条："]
    for index, entry in enumerate(entries, 1):
        if not isinstance(entry, dict):
            continue
        entry_id = str(entry.get("id") or "")
        scope = str(entry.get("scope") or "")
        keys = "、".join(str(k) for k in (entry.get("keys") or []) if str(k))
        content = _entry_preview(entry.get("content"))
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


def _find_entry(entries: list[dict], entry_id: str) -> dict | None:
    for entry in entries:
        if isinstance(entry, dict) and str(entry.get("id") or "") == entry_id:
            return entry
    return None


def update(entry_id: str, new_content: str) -> str:
    path = config.WORLD_INFO_PATH
    data = _load(path)
    entries = data.get("entries", [])
    if not isinstance(entries, list):
        return "错误：世界书数据损坏"
    entry = _find_entry(entries, entry_id)
    if entry is None:
        return f"错误：未找到条目 {entry_id}"
    new_content = str(new_content or "").strip()
    if not new_content:
        return "错误：新内容不能为空"
    entry["content"] = new_content
    entry["updated_at"] = _now_iso()
    _save(path, data)
    return f"已更新世界书条目 {entry_id}"


def delete(entry_id: str) -> str:
    path = config.WORLD_INFO_PATH
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


def enable(entry_id: str, enabled_str: str) -> str:
    path = config.WORLD_INFO_PATH
    data = _load(path)
    entries = data.get("entries", [])
    if not isinstance(entries, list):
        return "错误：世界书数据损坏"
    entry = _find_entry(entries, entry_id)
    if entry is None:
        return f"错误：未找到条目 {entry_id}"
    enabled = str(enabled_str or "").strip().lower() in ("true", "1", "yes", "开", "启用")
    entry["enabled"] = enabled
    entry["updated_at"] = _now_iso()
    _save(path, data)
    return f"世界书条目 {entry_id} 已{'启用' if enabled else '禁用'}"

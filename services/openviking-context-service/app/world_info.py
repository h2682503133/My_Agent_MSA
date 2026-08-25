"""世界书（World Info）本地文件检索。

存储（与 PROCESS 同级，NFS config/orchestrator/config/world_info/）：
- world_info.json：条目列表
- groups.json：agent_id → 群组id 映射（"agent_id : 群组id" 方式）

匹配规则（无全局作用域）：
- scope 语义：
  - agent_id        ：仅该智能体可见（工具/命令写入时默认填当前 agent）
  - "group:群组id"  ：该群组内所有智能体可见（群组在 groups.json 中定义）
  - 空 / 缺失        ：不参与匹配（视为无效条目；管理命令/工具写入时自动填 scope）
- 触发：constant 常驻条目全部入选；其余按 query + recent_messages 做关键词子串/正则匹配
- match_mode：条目级 or（任一关键词命中即触发，默认）/ and（全部关键词命中才触发）
- 排序：priority 降序，同优先级 updated_at 新者在前
- 裁剪：max_entries 上限 + max_tokens 预算（自高优先级起累计）

纯本地文件 + 正则，不依赖 OpenViking server / embedding，
因此 embedding 降级（keyword_recall）不影响本功能。
"""

from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path

from app import config
from app.logger import debug_log


# ─── 文件缓存（mtime 失效，用户改 NFS 后下一次请求即生效）────────

_cache_lock = threading.Lock()
_cache: dict[str, tuple[float, object]] = {}  # path -> (mtime, data)


def _load_json_cached(path: Path, default):
    """按 mtime 缓存读取 JSON 文件；文件不存在 / 解析失败返回 default。"""
    try:
        stat = path.stat()
        mtime = stat.st_mtime
    except OSError:
        _cache.pop(str(path), None)
        return default
    key = str(path)
    with _cache_lock:
        cached = _cache.get(key)
        if cached and cached[0] == mtime:
            return cached[1]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        debug_log(f"world_info: 读取 {path} 失败: {exc}")
        data = default
    with _cache_lock:
        _cache[key] = (mtime, data)
    return data


def _load_store() -> tuple[list[dict], dict[str, list[str]]]:
    """读取世界书条目 + 群组映射。"""
    raw = _load_json_cached(config.WORLD_INFO_PATH, {})
    entries = raw.get("entries", []) if isinstance(raw, dict) else []
    if not isinstance(entries, list):
        entries = []

    groups_raw = _load_json_cached(config.WORLD_INFO_GROUPS_PATH, {})
    agent_groups: dict[str, list[str]] = {}
    if isinstance(groups_raw, dict):
        mapping = groups_raw.get("agent_groups", groups_raw.get("groups", {}))
        if isinstance(mapping, dict):
            for agent_id, gids in mapping.items():
                if isinstance(gids, str):
                    gids = [gids]
                if isinstance(gids, list):
                    agent_groups[str(agent_id)] = [str(g) for g in gids]
    return entries, agent_groups


def _estimate_tokens(text: str) -> int:
    """粗略 token 估算：CJK 约 1 字/token，拉丁约 4 字符/token，取 len//2 折中。"""
    return max(1, int(len(str(text or "")) // 2))


def _scope_matches(scope: str, agent_id: str, agent_groups: dict[str, list[str]]) -> bool:
    """scope 是否对当前 agent 可见（无全局：空 scope 一律不匹配）。"""
    scope = str(scope or "").strip()
    if not scope:
        return False
    if scope == agent_id:
        return True
    if scope.startswith("group:"):
        group_id = scope[len("group:"):].strip()
        return group_id in (agent_groups.get(agent_id) or [])
    return False


def search_world_info(
    user_id: str,
    agent_id: str,
    query: str,
    recent_messages: list[str],
    max_tokens: int = 0,
    max_entries: int = 0,
) -> dict:
    """返回命中条目（已排序裁剪）：{"hits": [...], "error": ""}。"""
    try:
        entries, agent_groups = _load_store()
    except Exception as exc:
        return {"hits": [], "error": f"world_info load failed: {exc}"}

    budget = int(max_tokens or 0) or config.WORLD_INFO_MAX_TOKENS
    cap = int(max_entries or 0) or config.WORLD_INFO_MAX_ENTRIES

    # 触发源文本：当前消息 + 最近消息
    sources = [str(query or "")]
    for msg in (recent_messages or []):
        if msg:
            sources.append(str(msg))
    source_text = "\n".join(sources).lower()

    matched: list[dict] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if not entry.get("enabled", True):
            continue
        scope = str(entry.get("scope") or "").strip()
        if not _scope_matches(scope, agent_id, agent_groups):
            continue

        is_constant = bool(entry.get("constant", False))
        keys = entry.get("keys") or []
        if not isinstance(keys, list):
            keys = [keys]
        keys = [str(k).strip() for k in keys if str(k).strip()]
        regex_mode = bool(entry.get("regex", False))
        # 匹配模式：or = 任一命中即触发（默认）；and = 全部命中才触发
        match_mode = str(entry.get("match_mode") or "or").strip().lower()
        if match_mode not in ("or", "and"):
            match_mode = "or"
        require_all = match_mode == "and"

        hit = False
        if is_constant:
            hit = True
        elif regex_mode:
            results = []
            for key in keys:
                try:
                    results.append(bool(re.search(key, source_text, re.IGNORECASE)))
                except re.error:
                    results.append(False)
            hit = all(results) if require_all else any(results)
        else:
            if require_all:
                hit = all(key.lower() in source_text for key in keys)
            else:
                hit = any(key.lower() in source_text for key in keys)

        if not hit:
            continue

        content = str(entry.get("content") or "").strip()
        if not content:
            continue

        matched.append({
            "entry_id": str(entry.get("id") or ""),
            "keys": keys,
            "content": content,
            "priority": int(entry.get("priority", 0) or 0),
            "constant": is_constant,
            "token_count": _estimate_tokens(content),
            "_updated_at": str(entry.get("updated_at") or entry.get("created_at") or ""),
        })

    # 排序：priority 降序，同优先级 updated_at 新者在前（reverse=True 对复合键整体降序）
    matched.sort(key=lambda h: (h["priority"], h["_updated_at"]), reverse=True)

    # 裁剪：max_entries 上限
    if cap > 0:
        matched = matched[:cap]

    # 裁剪：max_tokens 预算（自高优先级起累计）
    total = 0
    kept: list[dict] = []
    for h in matched:
        if budget > 0 and total + h["token_count"] > budget:
            continue
        total += h["token_count"]
        kept.append(h)

    hits = []
    for h in kept:
        hits.append({
            "entry_id": h["entry_id"],
            "keys": h["keys"],
            "content": h["content"],
            "priority": h["priority"],
            "constant": h["constant"],
            "token_count": h["token_count"],
        })
    return {"hits": hits, "error": ""}

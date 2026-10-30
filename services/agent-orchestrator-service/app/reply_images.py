"""回复关键词 → 自动配图（orchestrator 侧）。

规则按**智能体**存：`config/reply_images/<agent_id>.json`
（tool-runtime 里的 `reply-image` 工具写同一个文件，见 app/reply_images_tool.py 的说明）。

匹配口径：
- 用**子串**匹配（不解释正则），ASCII 部分大小写不敏感；
- 多条规则命中时取「在回复正文里出现位置最靠前」的那条；位置相同按规则顺序；
- 每条规则一组关键词，只发一张图（一次回复最多发一张，避免刷屏）。
"""

from __future__ import annotations

import datetime
import json
import os
import re
from pathlib import Path
from typing import Any

from app import config
from app.logger import debug_log

# 关键词分隔符：中英文逗号、顿号、分号、竖线（竖线其实已被上游切参用掉，兜底）
# 全局规则目录名（作用于所有用户）；用下划线开头避免与真实 user_id 冲突
GLOBAL_SCOPE_DIR = "global"
# 指定"哪个用户的规则对所有人生效"（存在 .reply_images/_source.json）
SOURCE_CONFIG_NAME = "_source.json"

_KEYWORD_SPLIT_RE = re.compile(r"[,，、;；|]+")


def rules_dir() -> Path:
    raw = os.getenv("REPLY_IMAGES_DIR", "")
    # 以点开头：dashboard 的配置管理会自动跳过这种目录
    return Path(raw) if raw else config.CONFIG_DIR / ".reply_images"


def _safe_segment(value: str, default: str = "main") -> str:
    raw = str(value or default).strip() or default
    safe = re.sub(r"[^0-9A-Za-z_.@-]+", "_", raw).strip("._") or default
    return safe[:64]


def _path_for(agent_id: str, user_id: str = "") -> Path:
    """规则文件：`<rules_dir>/<user_id>/<agent_id>.json`（按用户 → 智能体隔离）。

    `user_id=GLOBAL_SCOPE_DIR` 即全局规则（作用于所有用户）。
    """
    return (
        rules_dir()
        / _safe_segment(user_id or "default", default="default")
        / f"{_safe_segment(agent_id)}.json"
    )


def _source_path() -> Path:
    return rules_dir() / SOURCE_CONFIG_NAME


def get_source_user() -> str:
    """读取"全局来源用户"；未设置返回空串。"""
    path = _source_path()
    if not path.exists():
        return ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return ""
    return str(data.get("source_user") or "").strip()


def set_source_user(user_id: str) -> str:
    """设置/清除全局来源用户（空串 = 清除）。"""
    target = str(user_id or "").strip()
    path = _source_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not target:
        if path.exists():
            path.unlink()
        return "已清除全局来源用户（规则不再对所有人生效）"
    path.write_text(
        json.dumps({"source_user": target}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return f"已把用户 {target} 的规则设为全局（对其余所有用户生效）"


def load_rules(
    agent_id: str,
    user_id: str = "",
    *,
    include_global: bool = True,
) -> list[dict[str, Any]]:
    """读规则；默认把全局规则拼在**后面**（用户规则优先命中）。

    `include_global=False` 只读该用户自己的（给工具做“只改自己”的语义用）。
    """
    own = _load_file(agent_id, user_id)
    if not include_global or (user_id or "default") == GLOBAL_SCOPE_DIR:
        return own

    merged = list(own)
    seen_images = {rule["image"] for rule in own}

    # 1) 指定的"全局来源用户"：他设的规则对其余人就是全局
    source = get_source_user()
    if source and source != (user_id or "default"):
        for rule in _load_file(agent_id, source):
            if rule["image"] in seen_images:
                continue
            merged.append({**rule, "scope": "global", "owner": source})
            seen_images.add(rule["image"])

    # 2) 兼容旧的 global 目录（显式写 global 的规则）
    for rule in _load_file(agent_id, GLOBAL_SCOPE_DIR):
        if rule["image"] in seen_images:
            continue
        merged.append({**rule, "scope": "global"})
        seen_images.add(rule["image"])
    return merged


def _load_file(agent_id: str, user_id: str = "") -> list[dict[str, Any]]:
    path = _path_for(agent_id, user_id)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        debug_log(f"[reply-image] 规则文件损坏，忽略：{path} ({exc})")
        return []
    rules = data.get("rules") if isinstance(data, dict) else None
    if not isinstance(rules, list):
        return []
    cleaned: list[dict[str, Any]] = []
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        keywords = [str(k).strip() for k in (rule.get("keywords") or []) if str(k).strip()]
        image = str(rule.get("image") or "").strip()
        if not keywords or not image:
            continue
        cleaned.append({
            "keywords": keywords,
            "image": image,
            "note": str(rule.get("note") or ""),
            "updated_at": str(rule.get("updated_at") or ""),
            "scope": "global" if (user_id or "default") == GLOBAL_SCOPE_DIR else "user",
        })
    return cleaned


def save_rules(agent_id: str, rules: list[dict[str, Any]], user_id: str = "") -> Path:
    path = _path_for(agent_id, user_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"agent_id": agent_id, "rules": rules}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def split_keywords(text: str) -> list[str]:
    seen: list[str] = []
    for part in _KEYWORD_SPLIT_RE.split(str(text or "")):
        keyword = part.strip()
        if keyword and keyword not in seen:
            seen.append(keyword)
    return seen


def upsert_rule(agent_id: str, keywords: list[str], image: str, note: str = "", user_id: str = "") -> str:
    """新增/覆盖一组关键词对应的图片（同图同组则合并关键词）。

    **只改该用户自己的文件**（读的是 `_load_file`，不带全局），
    否则会把拼进来的全局规则一起写进用户文件，造成重复。
    """
    rules = _load_file(agent_id, user_id)
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    for rule in rules:
        if rule["image"] == image:
            merged = list(rule["keywords"])
            for keyword in keywords:
                if keyword not in merged:
                    merged.append(keyword)
            rule["keywords"] = merged
            if note:
                rule["note"] = note
            rule["updated_at"] = stamp
            save_rules(agent_id, rules, user_id)
            return f"已更新规则（同图合并关键词）：{('、'.join(merged))} → {image}"
    rules.append({"keywords": keywords, "image": image, "note": note, "updated_at": stamp})
    save_rules(agent_id, rules, user_id)
    return (
        f"已添加规则 #{len(rules)}：{('、'.join(keywords))} → {image}"
        f"（共 {len(rules)} 条，命中时按回复里出现位置最靠前的那条发图）"
    )


def remove_rule(agent_id: str, keyword: str, user_id: str = "") -> str:
    """按关键词删除：命中该关键词的规则整条删掉（只动自己的文件）。"""
    rules = _load_file(agent_id, user_id)
    target = str(keyword or "").strip()
    if not target:
        return "错误：删除需要给出关键词"
    kept: list[dict[str, Any]] = []
    removed = 0
    for rule in rules:
        if target in rule["keywords"] or target.casefold() in [k.casefold() for k in rule["keywords"]]:
            removed += 1
            continue
        kept.append(rule)
    if not removed:
        return f"没有找到包含关键词「{target}」的规则（当前共 {len(rules)} 条）"
    save_rules(agent_id, kept, user_id)
    return f"已删除 {removed} 条包含「{target}」的规则（剩余 {len(kept)} 条）"


def list_rules(agent_id: str, user_id: str = "") -> str:
    own = _load_file(agent_id, user_id)
    source = get_source_user()
    global_rules = _load_file(agent_id, source) if source and source != (user_id or "default") else []
    legacy_global = _load_file(agent_id, GLOBAL_SCOPE_DIR)
    if legacy_global:
        global_rules = list(global_rules) + list(legacy_global)
    if not own and not global_rules:
        return (
            f"当前智能体 {agent_id} 还没有回复配图规则。"
            "用法：工具调用:reply-image|关键词|图片（第 3 参写 global 表示对所有人生效）"
        )
    lines = [f"智能体 {agent_id} 的回复配图规则："]
    if own:
        lines.append(f"你自己的（{len(own)} 条，优先命中）：")
        for index, rule in enumerate(own, 1):
            note = f"（{rule['note']}）" if rule.get("note") else ""
            lines.append(f"  {index}. {('、'.join(rule['keywords']))} → {rule['image']}{note}")
    if global_rules:
        label = f"全局（来源用户 {source}，{len(global_rules)} 条）" if source else f"全局（{len(global_rules)} 条）"
        lines.append(f"{label}：")
        for index, rule in enumerate(global_rules, 1):
            note = f"（{rule['note']}）" if rule.get("note") else ""
            lines.append(f"  {index}. {('、'.join(rule['keywords']))} → {rule['image']}{note}")
    return "\n".join(lines)


def match_first(
    text: str,
    rules: list[dict[str, Any]] | None = None,
    agent_id: str = "main",
    user_id: str = "",
) -> dict[str, Any] | None:
    """返回正文里**最先出现**的那条规则（同位置按规则顺序）。"""
    body = str(text or "")
    if not body:
        return None
    rules = load_rules(agent_id, user_id) if rules is None else rules
    if not rules:
        return None
    folded = body.casefold()
    best: tuple[int, int, dict[str, Any]] | None = None
    for order, rule in enumerate(rules):
        for keyword in rule["keywords"]:
            position = folded.find(keyword.casefold())
            if position < 0:
                continue
            candidate = (position, order, rule)
            if best is None or candidate[:2] < best[:2]:
                best = candidate
            break
    return best[2] if best else None


def attach_images(task, text: str, agent_id: str = "") -> list[str]:
    """按匹配结果把图片挂到本次回复（追加到 task.send_images），返回新增的图片。"""
    agent = str(
        agent_id
        or getattr(task, "agent_id", "")
        or getattr(getattr(task, "default_agent", None), "id", "")
        or "main"
    )
    user = str(getattr(getattr(task, "user", None), "id", "") or "")
    hit = match_first(text, agent_id=agent, user_id=user)
    if not hit:
        return []
    image = hit["image"]
    images = task.send_images
    if image in images:
        return []
    images.append(image)
    debug_log(
        f"[{task.user.id}] [reply-image] 命中关键词 "
        f"{('、'.join(hit['keywords']))} → {image}"
    )
    return [image]

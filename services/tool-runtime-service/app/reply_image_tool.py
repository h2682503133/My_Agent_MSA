"""tool-runtime 侧：`reply-image` 工具，维护「回复关键词 → 自动配图」规则。

存储与 orchestrator 共用同一个 NFS 目录：
  orchestrator 视角  /app/config/reply_images/<agent>.json
  tool-runtime 视角  /app/system_prompts/orchestrator/config/reply_images/<agent>.json
所以这里默认由 SYSTEM_PROMPTS_DIR 推出路径，也可用 REPLY_IMAGES_DIR 覆盖。

用法（工具参数）：
  reply-image|关键词1,关键词2|图片    新增/更新一组关键词（图片可为 URL 或工作区路径）
  reply-image|关键词|                删除包含该关键词的规则
  reply-image||                      列出当前所有规则

规则按「用户 → 智能体」隔离：<rules_root>/<user_id>/<agent_id>.json
"""

from __future__ import annotations

import datetime
import json
import os
import re
from pathlib import Path
from typing import Any

GLOBAL_SCOPE_DIR = "global"
# 指定"哪个用户的规则对所有人生效"（.reply_images/_source.json）
SOURCE_CONFIG_NAME = "_source.json"

_KEYWORD_SPLIT_RE = re.compile(r"[,，、;；|]+")


def _safe_segment(value: str, default: str) -> str:
    safe = re.sub(r"[^0-9A-Za-z_.@-]+", "_", str(value or default).strip() or default).strip("._")
    return (safe or default)[:64]


def _path_for(rules_root: Path, agent_id: str, user_id: str = "") -> Path:
    """`<rules_root>/<user>/<agent>.json`（用户 → 智能体两级隔离）。"""
    return Path(rules_root) / _safe_segment(user_id, "default") / f"{_safe_segment(agent_id, 'main')}.json"


def split_keywords(text: str) -> list[str]:
    seen: list[str] = []
    for part in _KEYWORD_SPLIT_RE.split(str(text or "")):
        keyword = part.strip()
        if keyword and keyword not in seen:
            seen.append(keyword)
    return seen


def get_source_user(rules_root: Path) -> str:
    path = Path(rules_root) / SOURCE_CONFIG_NAME
    if not path.exists():
        return ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return ""
    return str(data.get("source_user") or "").strip()


def set_source_user(rules_root: Path, user_id: str) -> str:
    path = Path(rules_root) / SOURCE_CONFIG_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    target = str(user_id or "").strip()
    if not target:
        if path.exists():
            path.unlink()
        return "已清除全局来源用户"
    path.write_text(json.dumps({"source_user": target}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return f"已把用户 {target} 的规则设为全局（对其余所有用户生效）"


def load_rules(
    rules_root: Path,
    agent_id: str,
    user_id: str = "",
    *,
    include_global: bool = True,
) -> list[dict[str, Any]]:
    """读规则；默认把全局规则拼在后面（用户自己的优先）。"""
    own = _load_file(rules_root, agent_id, user_id)
    if not include_global or (user_id or "default") == GLOBAL_SCOPE_DIR:
        return own

    merged = list(own)
    seen = {rule["image"] for rule in own}

    source = get_source_user(rules_root)
    if source and source != (user_id or "default"):
        for rule in _load_file(rules_root, agent_id, source):
            if rule["image"] in seen:
                continue
            merged.append({**rule, "scope": "global", "owner": source})
            seen.add(rule["image"])

    for rule in _load_file(rules_root, agent_id, GLOBAL_SCOPE_DIR):
        if rule["image"] in seen:
            continue
        merged.append({**rule, "scope": "global"})
        seen.add(rule["image"])
    return merged


def _load_file(rules_root: Path, agent_id: str, user_id: str = "") -> list[dict[str, Any]]:
    path = _path_for(rules_root, agent_id, user_id)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
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


def save_rules(rules_root: Path, agent_id: str, rules: list[dict[str, Any]], user_id: str = "") -> Path:
    path = _path_for(rules_root, agent_id, user_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps({"agent_id": agent_id, "rules": rules}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, path)
    return path


def set_rule(
    rules_root: Path,
    agent_id: str,
    keywords_text: str,
    image: str,
    note: str = "",
    user_id: str = "",
) -> str:
    keywords = split_keywords(keywords_text)
    image = str(image or "").strip()
    if not keywords:
        return "错误：请给出关键词（多个用逗号分隔）"
    if not image:
        return "错误：请给出图片（图床 URL 或工作区里的图片路径）"

    rules = _load_file(rules_root, agent_id, user_id)
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
            save_rules(rules_root, agent_id, rules, user_id)
            return f"已更新规则（同一张图合并关键词）：{('、'.join(merged))} → {image}"
    rules.append({"keywords": keywords, "image": image, "note": note, "updated_at": stamp})
    save_rules(rules_root, agent_id, rules, user_id)
    return (
        f"已添加规则 #{len(rules)}：{('、'.join(keywords))} → {image}。"
        f"命中判定：回复里最先出现的关键词生效，只补一张图（当前共 {len(rules)} 条）"
    )


def remove_rule(rules_root: Path, agent_id: str, keyword: str, user_id: str = "") -> str:
    target = str(keyword or "").strip()
    if not target:
        return "错误：删除需要给出关键词"
    rules = _load_file(rules_root, agent_id, user_id)
    kept: list[dict[str, Any]] = []
    removed = 0
    for rule in rules:
        if target.casefold() in [k.casefold() for k in rule["keywords"]]:
            removed += 1
            continue
        kept.append(rule)
    if not removed:
        return f"没有包含「{target}」的规则（当前共 {len(rules)} 条）"
    save_rules(rules_root, agent_id, kept, user_id)
    return f"已删除 {removed} 条包含「{target}」的规则（剩余 {len(kept)} 条）"


def list_rules(rules_root: Path, agent_id: str, user_id: str = "") -> str:
    rules = load_rules(rules_root, agent_id, user_id)
    if not rules:
        return f"智能体 {agent_id} 还没有回复配图规则。用法：工具调用:reply-image|关键词|图片"
    lines = [f"智能体 {agent_id} 的回复配图规则（共 {len(rules)} 条）："]
    for index, rule in enumerate(rules, 1):
        note = f"（{rule['note']}）" if rule.get("note") else ""
        lines.append(f"{index}. {('、'.join(rule['keywords']))} → {rule['image']}{note}")
    return "\n".join(lines)


def publish_local_image(source: Path, base_url: str, asset_dir: Path) -> str:
    """把工作区里的图片拷进图床 PVC，返回集群内可访问的 URL。

    为什么必须转 URL：各渠道适配器是**按 URL 取图**的（实测
    `http://image-assets-service/assets/<name>` 在 qq-llbot / qq-official 两个 pod 里都 200）。
    """
    import mimetypes
    import shutil
    import uuid
    import urllib.parse

    if not source.exists() or not source.is_file():
        raise FileNotFoundError(f"图片不存在：{source}")

    asset_dir.mkdir(parents=True, exist_ok=True)
    suffix = source.suffix or mimetypes.guess_extension(mimetypes.guess_type(str(source))[0] or "") or ""
    target_name = f"reply-{uuid.uuid4().hex[:12]}{suffix}"
    shutil.copyfile(source, asset_dir / target_name)
    return f"{base_url.rstrip('/')}/{urllib.parse.quote(target_name)}"


def resolve_image(value: str, workspace_root: Path | None, base_url: str, asset_dir: Path) -> str:
    """把工具参数里的图片统一成 URL：http(s)/data URL 原样保留，本地路径转图床 URL。"""
    raw = str(value or "").strip()
    if not raw:
        return ""
    lowered = raw.lower()
    if lowered.startswith(("http://", "https://", "data:")):
        return raw
    if workspace_root is None:
        raise ValueError("本地图片路径需要工作区，请改用图床 URL")
    candidate = (Path(workspace_root) / raw).resolve() if not Path(raw).is_absolute() else Path(raw)
    try:
        candidate.relative_to(Path(workspace_root).resolve())
    except ValueError as exc:
        raise ValueError(f"图片路径必须在工作区内：{raw}") from exc
    return publish_local_image(candidate, base_url, asset_dir)


def handle(
    rules_root: Path,
    agent_id: str,
    args: list[str],
    kwargs: dict,
    *,
    user_id: str = "",
    workspace_root: Path | None = None,
    base_url: str = "",
    asset_dir: Path | None = None,
) -> str:
    keywords_text = kwargs.get("keywords") or kwargs.get("keyword") or (args[0] if args else "")
    image = kwargs.get("image") or kwargs.get("url") or (args[1] if len(args) > 1 else "")
    note = kwargs.get("note") or (args[2] if len(args) > 2 else "")

    keywords_text = str(keywords_text or "").strip()
    image = str(image or "").strip()

    # 特殊用法：关键词写 "@source" 时，图片参数即“全局来源用户”（空 = 清除）
    if keywords_text.strip().lower() in {"@source", "source"}:
        return set_source_user(rules_root, image)

    # 第 3 参可以是备注，也可以显式写 global / 全局（写全局规则，对所有人生效）
    scope_raw = str(note or "").strip().lower()
    is_global = scope_raw in {"global", "全局", "所有", "all"}
    if is_global:
        user_id = GLOBAL_SCOPE_DIR
        note = ""

    if not keywords_text and not image:
        return list_rules(rules_root, agent_id, user_id)
    if keywords_text and not image:
        return remove_rule(rules_root, agent_id, keywords_text, user_id)

    try:
        image_url = resolve_image(
            image,
            workspace_root,
            base_url,
            asset_dir or Path("/app/assets/images"),
        )
    except Exception as exc:  # noqa: BLE001
        return f"错误：{exc}"
    return set_rule(
        rules_root, agent_id, keywords_text, image_url, str(note or "").strip(), user_id
    )

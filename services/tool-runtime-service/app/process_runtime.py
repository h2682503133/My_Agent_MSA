"""PROCESS 长期事件记录存储读写（内置工具）。

存储：<PROCESS_DIR>/<user_id>/<agent_id>.json
结构：{"turn": N, "items": [{"title": "...", "content": "..."}], "vars": {key: value}}
      turn 仅轮次模式由 orchestrator 每次读取时递增维护，此处读写保持原样。

内置工具：
- process-write|index|title|content   index=-1 追加到末尾；1..N 覆写对应条目
- process-remove|index                按 1 起始删除对应条目
- process-init                        重置（有模板则载入模板，无模板清空条目/变量/轮次归零）
- process-modify|key|op               按 key 修改 vars 变量（严格匹配 → 别名匹配 → 脱靶新建）

模板：<PROCESS_DIR>/process_templates#/<agent_id>.json
      "process_templates#" 目录名含 '#'，任何 user_id 经 _safe_segment 都生成不了
      含 '#' 的段名，天然防止"邪门用户占用模板目录"。
      模板结构与运行文件一致（turn/items/vars/aliases），init 时整体载入。

别名（aliases）：反向映射 {别名: 存储key}（页面所见即所得，运行时直接命中）。
      仅存于模板/运行文件，不参与注入段展示（注入段只显示存储 key=value）。
"""

from __future__ import annotations

import datetime
import json
import os
import re
import tempfile
from pathlib import Path


def _now_iso() -> str:
    """UTC ISO8601（与历史消息的 created_at 同口径，渲染时再转本地时间）。"""
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _safe_segment(value: str, default: str = "default") -> str:
    """user_id / agent_id 转安全目录名，避免路径逃逸。"""
    raw = str(value or default).strip() or default
    safe = re.sub(r"[^0-9A-Za-z_.@-]+", "_", raw).strip("._") or default
    return "default" if safe in {".", ".."} else safe


def _process_path(process_dir: str, user_id: str, agent_id: str) -> Path:
    return Path(process_dir) / _safe_segment(user_id) / f"{_safe_segment(agent_id)}.json"


def _template_path(process_dir: str, agent_id: str) -> Path:
    """初始化模板路径：process/process_templates#/<agent_id>.json"""
    return Path(process_dir) / "process_templates#" / f"{_safe_segment(agent_id)}.json"


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


def write(
    process_dir: str,
    user_id: str,
    agent_id: str,
    index: str,
    title: str,
    content: str,
    max_items: int = 0,
    max_chars: int = 0,
) -> str:
    """写一条长期事件记录（max_items / max_chars 来自该智能体 process*.md，0=不限）。

    超限一律显式报错：静默截断/丢弃会让模型以为已经写进去了。
    """
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

    if max_chars and len(content) > max_chars:
        return (
            f"错误：单条正文 {len(content)} 字，超过上限 {max_chars} 字"
            f"（在 process.md 里用 `max_chars: N` 调整）。请精简后再写。"
        )

    entry = {"title": title, "content": content}

    if idx == -1 or idx == len(items) + 1:
        if max_items and len(items) >= max_items:
            return (
                f"错误：存储已满（{len(items)}/{max_items} 条，上限在 process.md 里用 "
                f"`max_items: N` 调整）。请先用 process-modify 改写旧条目或 process-remove 删除。"
            )
        entry["created_at"] = _now_iso()
        items.append(entry)
        _save(path, data)
        used = f"（{len(items)}/{max_items} 条）" if max_items else f"（共 {len(items)} 条）"
        return f"已追加第 {len(items)} 条{used}：{title or '(无标题)'}"

    if 1 <= idx <= len(items):
        previous = items[idx - 1]
        # 覆写保留原记录时间（"这条何时建立"），老数据没时间就补当前
        if isinstance(previous, dict) and previous.get("created_at"):
            entry["created_at"] = previous["created_at"]
        else:
            entry["created_at"] = _now_iso()
        items[idx - 1] = entry
        _save(path, data)
        return f"已覆写第 {idx} 条：{title or '(无标题)'}"

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
    """重置 PROCESS：有模板则载入模板（含 items/vars/aliases），无模板清空条目/变量/轮次归零。"""
    path = _process_path(process_dir, user_id, agent_id)
    template = _template_path(process_dir, agent_id)
    if template.exists():
        try:
            tpl = json.loads(template.read_text(encoding="utf-8"))
            if isinstance(tpl, dict):
                clean = {"turn": 0}
                if isinstance(tpl.get("items"), list):
                    clean["items"] = tpl["items"]
                else:
                    clean["items"] = []
                if isinstance(tpl.get("vars"), dict):
                    clean["vars"] = tpl["vars"]
                if isinstance(tpl.get("aliases"), dict):
                    clean["aliases"] = tpl["aliases"]
                _save(path, clean)
                return f"已按模板初始化 PROCESS（{len(clean['items'])} 条事件，{len(clean.get('vars', {}))} 个变量）"
        except Exception:
            pass
    _save(path, {"turn": 0, "items": []})
    return "已初始化 PROCESS（无模板，条目已清空，轮次归零）"


# ─── vars 变量（按 key 修改）─────────────────────────────────

# 操作数：+10 / -5 / =40 / 40（脱靶新建时按符号数值存储：-5 存 -5.0，+10 存 10.0）
_OP_RE = re.compile(r"^\s*([+\-=]?)\s*(\d+)\s*$")


def _parse_op(op: str):
    """解析操作数 → (sign, num)；sign ∈ {'+','-','=',''}。非法返回 None。"""
    m = _OP_RE.match(str(op or "").strip())
    if not m:
        return None
    return m.group(1), float(m.group(2))


def _vars_get(data: dict) -> dict:
    vars_map = data.get("vars")
    if not isinstance(vars_map, dict):
        vars_map = {}
        data["vars"] = vars_map
    return vars_map


def _aliases_get(data: dict) -> dict:
    aliases = data.get("aliases")
    if not isinstance(aliases, dict):
        aliases = {}
        data["aliases"] = aliases
    return aliases


def _locate_key(vars_map: dict, aliases: dict, key: str) -> str | None:
    """定位存储 key：严格匹配 vars → 别名映射命中 → 无。

    - 严格匹配：key 直接是 vars 的存储 key
    - 别名匹配：aliases[别名] = 存储key（反向映射，运行时直接命中）
    - 都没有 → None（脱靶）
    agent 自建变量无别名 → 严格脱靶后第二步必为空 → 调用方按新建处理。
    """
    key = str(key or "").strip()
    if not key:
        return None
    if key in vars_map:
        return key
    if key in aliases:
        return str(aliases[key])
    return None


def modify(process_dir: str, user_id: str, agent_id: str, key: str, op: str) -> str:
    """按 key 修改 vars 变量：严格匹配 → 别名匹配 → 脱靶新建。

    命中且原值为数值（float）：
    - op 必须是数字运算 +N / -N / =N / N（无符号 N 视为 =N，防呆）
    - 数值变量不可改为非 float：op 为空或非数字字符 → 报错「请使用 =新值」

    命中且原值为字符/NULL：
    - 允许改为任意：空 → NULL；数字 → float（-5 → -5.0，+10 → 10.0）；字符 → 字符串原样

    脱靶（新建变量）：
    - op 为空 → 存 NULL
    - op 为数字（含符号）→ 存 float（-5 存 -5.0，+10 存 10.0）
    - op 为其他字符 → 存字符串原样
    """
    path = _process_path(process_dir, user_id, agent_id)
    data = _load(path)
    vars_map = _vars_get(data)
    aliases = _aliases_get(data)

    key = str(key or "").strip()
    if not key:
        return "错误：key 不能为空"
    op_str = str(op or "").strip()

    store_key = _locate_key(vars_map, aliases, key)
    if store_key is not None:
        old = vars_map.get(store_key)
        # 旧值类型判定：数值 → 强制数字运算（不可改为非 float）；字符/NULL → 允许改任意
        try:
            old_num = float(str(old).strip())
        except (TypeError, ValueError):
            old_num = None

        if old_num is not None:
            # 原为数值：op 必须是数字运算（+N / -N / =N / N），禁止改为非 float
            parsed = _parse_op(op_str)
            if parsed is None:
                return f"错误：{store_key} 是数值变量，不能用 '{op_str or '(空)'}' 修改，请使用 =新值 进行修改"
            sign, num = parsed
            if sign == "+":
                new = old_num + num
            elif sign == "-":
                new = old_num - num
            else:  # '=' 或 ''（无符号 N 视为 =N）
                new = num
            vars_map[store_key] = new
            _save(path, data)
            return f"已更新 {store_key}: {old} → {new}"

        # 原为字符/NULL：允许改为任意（空→NULL；数字→float；字符→字符串原样）
        if not op_str:
            new_val = None
        else:
            parsed = _parse_op(op_str)
            if parsed is not None:
                sign, num = parsed
                new_val = -num if sign == "-" else num
            else:
                new_val = op_str
        vars_map[store_key] = new_val
        _save(path, data)
        shown = "NULL" if new_val is None else new_val
        return f"已更新 {store_key}: {old} → {shown}"

    # 脱靶 → 新建（空 → NULL；数字 → float 按符号；字符 → 字符串原样）
    if not op_str:
        new_val = None
    else:
        parsed = _parse_op(op_str)
        if parsed is not None:
            sign, num = parsed
            new_val = -num if sign == "-" else num
        else:
            new_val = op_str
    vars_map[key] = new_val
    _save(path, data)
    shown = "NULL" if new_val is None else new_val
    return f"不存在变量{key}，现已新建 变量 {key} = {shown}"


def var_remove(process_dir: str, user_id: str, agent_id: str, key: str) -> str:
    """删除 vars 变量（严格匹配 → 别名匹配；脱靶报错，不新建）。"""
    path = _process_path(process_dir, user_id, agent_id)
    data = _load(path)
    vars_map = _vars_get(data)
    aliases = _aliases_get(data)

    key = str(key or "").strip()
    if not key:
        return "错误：key 不能为空"
    store_key = _locate_key(vars_map, aliases, key)
    if store_key is None:
        return f"错误：vars 中没有 '{key}'（现有：{', '.join(vars_map.keys()) or '无'}）"
    removed = vars_map.pop(store_key)
    _save(path, data)
    return f"已删除变量 {store_key}（原值 {removed}）"


def var_list(process_dir: str, user_id: str, agent_id: str) -> str:
    """列出全部 vars（存储 key = 值，平铺一行）。"""
    path = _process_path(process_dir, user_id, agent_id)
    data = _load(path)
    vars_map = _vars_get(data)
    if not vars_map:
        return "（vars 为空）"
    return "；".join(f"{k}={v}" for k, v in vars_map.items())

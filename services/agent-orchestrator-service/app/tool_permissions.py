"""工具权限分级与拦截（L0–L4）。

用户口径（2026-10-31 确定）：
  L0 公开 public       —— 所有用户可用
  L1 可屏蔽 soft       —— 所有用户可用，但**按用户**黑名单可整体屏蔽
  L2 白名单·配置 config —— 全局/跨用户配置与知识写
  L3 白名单·执行 exec   —— 容器/外部 VM 内执行、外发、外部资源、破坏性删除
  L4 白名单·宿主 host   —— 穿透到 Windows 宿主机
  （没有 L5；dashboard 是密码级特权通道，不经过本模块）

配置落在 system_settings.json 的 `tool_permission`：
  {
    "L1_blacklist_users": ["..."],   # 被屏蔽 L1 的用户（按用户，不按工具）
    "L2_whitelist_users": ["..."],
    "L3_whitelist_users": ["..."],
    "L4_whitelist_users": ["..."]
  }

提示词口径：只告诉模型「当前能用哪些工具」和「有个工具可以操作黑名单」，
**不解释分级标准，也不列出无权使用的工具**；被拦时才在报错里说明。
"""

from __future__ import annotations

import json
from typing import Any

from app import config
from app.context_builder import load_system_settings
from app.logger import debug_log

LEVEL_NAMES = {
    0: "公开",
    1: "可屏蔽",
    2: "白名单·配置",
    3: "白名单·执行",
    4: "白名单·宿主",
}

SETTINGS_KEY = "tool_permission"
# 名单里写 `*` 表示该档对所有用户开放（对应旧「关闭限制 = 所有人可用」）
WILDCARD = "*"
BLACKLIST_TOOL = "tool-blacklist"

# 档位标签：写在 TOOL.json 每个工具的 `groups` 里（`L0`–`L4`），代码不再硬编码工具名。
LEVEL_TOKENS = {"L0": 0, "L1": 1, "L2": 2, "L3": 3, "L4": 4}
DEFAULT_LEVEL = 4  # 没写 L 标签 → fail-closed

# 只做名字归一（不含档位信息）：dispatch 名 → TOOL.json 里的条目名
NAME_ALIASES = {
    "command": "shell",
    "run-shell": "shell",
    "download-file": "download",
    "delete_file": "delete-file",
}

_catalog_cache: dict[str, tuple] = {}


def _catalog_path():
    try:
        from app.context_builder import TOOL_CATALOG_FILENAME

        return config.SYSTEM_PROMPT_DIR / TOOL_CATALOG_FILENAME
    except Exception:  # noqa: BLE001
        return config.SYSTEM_PROMPT_DIR / "TOOL.json"


def _catalog() -> dict:
    """读 TOOL.json（按 mtime+size 缓存，改文件即时生效）。"""
    path = _catalog_path()
    try:
        stat = path.stat()
    except OSError:
        return {}
    key = str(path)
    stamp = (stat.st_mtime_ns, stat.st_size)
    cached = _catalog_cache.get(key)
    if cached and cached[0] == stamp:
        return cached[1]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        debug_log(f"[permission] 读取 TOOL.json 失败（按无标签处理）: {exc}")
        data = {}
    if not isinstance(data, dict):
        data = {}
    _catalog_cache[key] = (stamp, data)
    return data


def _tool_entries(name: str) -> list[dict]:
    """按名字找工具条目；`run-shell`/`command` 这类别名用后缀匹配归到 `shell`。"""
    wanted = str(name or "").strip().lower()
    if not wanted:
        return []
    wanted = NAME_ALIASES.get(wanted, wanted)
    catalog = _catalog()
    exact: list[dict] = []
    fuzzy: list[dict] = []
    for section in catalog.get("sections") or []:
        for tool in section.get("tools") or []:
            if not isinstance(tool, dict):
                continue
            tool_name = str(tool.get("name") or "").strip().lower()
            if not tool_name:
                continue
            if tool_name == wanted:
                exact.append(tool)
            elif wanted.endswith("-" + tool_name) or wanted.endswith("_" + tool_name) or tool_name.startswith(wanted + "-"):
                fuzzy.append(tool)
    return exact or fuzzy


def _entry_level(tool: dict) -> int:
    """工具条目自己的档位：groups 里最严格的 L 标签；没有则 fail-closed。"""
    levels = []
    for item in tool.get("groups") or []:
        token = str(item).strip().upper()
        if token in LEVEL_TOKENS:
            levels.append(LEVEL_TOKENS[token])
    return max(levels) if levels else DEFAULT_LEVEL


def level_of(tool_name: str) -> int:
    """工具基础档位 = 同名条目里最低的那档；查不到条目 → fail-closed（L4）。"""
    entries = _tool_entries(tool_name)
    if not entries:
        return DEFAULT_LEVEL
    return min(_entry_level(tool) for tool in entries)


# 技能执行（动态技能名不在目录里）：默认按 L1 处理
SKILL_LEVEL = 1


def _settings() -> dict[str, Any]:
    data = load_system_settings()
    section = data.get(SETTINGS_KEY)
    return section if isinstance(section, dict) else {}


def _user_list(section: dict[str, Any], key: str) -> set[str]:
    values = section.get(key) or []
    if not isinstance(values, list):
        return set()
    return {str(item).strip() for item in values if str(item).strip()}


READ_METHODS = {"GET", "HEAD", "OPTIONS"}


def _effective_level(tool_name: str, args: list[str], kwargs: dict[str, str]) -> int:
    """同名多条时按调用形态取档：只读形态取最低档，写形态取最高档（不认工具名）。"""
    entries = _tool_entries(tool_name)
    if not entries:
        return DEFAULT_LEVEL
    levels = sorted({_entry_level(tool) for tool in entries})
    if len(levels) == 1:
        return levels[0]
    method = str(kwargs.get("method") or (args[1] if len(args) > 1 else "")).strip().upper()
    body = kwargs.get("data") or (args[2] if len(args) > 2 else "")
    read_like = (not method or method in READ_METHODS) and not str(body).strip()
    return levels[0] if read_like else levels[-1]


def check(user_id: str, tool_name: str, args: list[str] | None = None, kwargs: dict[str, str] | None = None) -> tuple[bool, str]:
    """判定某用户能否用某工具。返回 (allowed, 拒绝原因)。"""
    user = str(user_id or "").strip()
    name = str(tool_name or "").strip()
    if not name:
        return False, "工具名为空"
    if name == BLACKLIST_TOOL:
        return True, ""      # 黑名单工具本身永远可用（否则被屏蔽的人无法自救）

    section = _settings()
    level = _effective_level(name, list(args or []), dict(kwargs or {}))

    if level == 0:
        return True, ""
    if level == 1:
        if user and user in _user_list(section, "L1_blacklist_users"):
            return False, (
                f"工具 {name} 被拦截：当前用户 {user} 在黑名单内（L1 可屏蔽档）。"
                "需要恢复请联系管理员在 dashboard「工具权限」页解除。"
            )
        return True, ""

    key = f"L{level}_whitelist_users"
    allowed_users = _user_list(section, key)
    if WILDCARD in allowed_users:
        return True, ""
    if user and user in allowed_users:
        return True, ""
    return False, (
        f"工具 {name} 被拦截：需要 L{level}（{LEVEL_NAMES.get(level, '受限')}）权限，"
        f"当前用户 {user or '未识别'} 不在名单内。"
    )


def blacklist_users() -> list[str]:
    return sorted(_user_list(_settings(), "L1_blacklist_users"))


def whitelist_users(level: int) -> list[str]:
    return sorted(_user_list(_settings(), f"L{level}_whitelist_users"))


def _write_section(section: dict[str, Any]) -> str:
    path = config.SYSTEM_SETTINGS_PATH
    data: dict[str, Any] = {}
    try:
        if path.exists():
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
    except Exception as exc:  # noqa: BLE001
        return f"错误：读取 system_settings.json 失败（{exc}）"
    data[SETTINGS_KEY] = section
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        return f"错误：写入 system_settings.json 失败（{exc}）"
    return ""


def set_blacklist(user_id: str, blocked: bool) -> str:
    """把用户加入/移出 L1 黑名单（供 tool-blacklist 工具调用）。"""
    user = str(user_id or "").strip()
    if not user:
        return "错误：需要用户 id"
    section = dict(_settings())
    current = sorted(_user_list(section, "L1_blacklist_users"))
    if blocked:
        if user in current:
            return f"用户 {user} 已在黑名单内"
        current.append(user)
        message = f"已把用户 {user} 加入操作黑名单（L1 档工具将不可用）"
    else:
        if user not in current:
            return f"用户 {user} 不在黑名单内"
        current = [item for item in current if item != user]
        message = f"已把用户 {user} 移出操作黑名单"
    section["L1_blacklist_users"] = current
    error = _write_section(section)
    if error:
        return error
    debug_log(f"[permission] 黑名单变更：{user} blocked={blocked}")
    return message


def level_notes() -> dict[str, str]:
    """「工具权限」页里按档填写、注入到全局设定末尾的说明（L0–L4）。"""
    raw = _settings().get("level_notes")
    if not isinstance(raw, dict):
        return {}
    notes: dict[str, str] = {}
    for key in ("ALL", "L0", "L1", "L2", "L3", "L4"):
        value = str(raw.get(key) or "").strip()
        if value:
            notes[key] = value
    return notes


DEFAULT_BLACKLIST_HINT = (
    "如需缩小自己的操作范围，可调用 `工具调用:tool-blacklist|用户id|` 把该用户加入黑名单"
    "（只能加入，解除需要管理员操作）。"
)
DEFAULT_BLOCKED_HINT = (
    "注意：当前用户的操作范围被限制，只能做上面这些查询/读取类操作；需要恢复请联系管理员。"
)


def blacklist_hints() -> tuple[str, str]:
    """(正常用户提示, 已被限制用户提示)；未配置时用内置默认文案。"""
    section = _settings()
    normal = str(section.get("blacklist_hint") or "").strip() or DEFAULT_BLACKLIST_HINT
    blocked = str(section.get("blacklist_blocked_hint") or "").strip() or DEFAULT_BLOCKED_HINT
    return normal, blocked


def available_levels(user_id: str) -> list[int]:
    """该用户实际可用的档位（L0 恒有；L1 除非被黑名单；L2–L4 看白名单/通配）。"""
    section = _settings()
    levels = [0]
    if str(user_id or "").strip() not in _user_list(section, "L1_blacklist_users"):
        levels.append(1)
    for level in (2, 3, 4):
        allowed = _user_list(section, f"L{level}_whitelist_users")
        if WILDCARD in allowed or (user_id and str(user_id).strip() in allowed):
            levels.append(level)
    return levels


def describe_for_prompt(user_id: str) -> str:
    """给提示词用的一段说明：只讲「当前能用什么」+ 黑名单工具，不暴露分级标准。"""
    user = str(user_id or "").strip()
    levels = set(available_levels(user))
    # 工具清单直接从 TOOL.json 里挑（档位来自 groups 标签），同名多条取可用那一档
    available: list[str] = []
    seen: set[str] = set()
    for section in _catalog().get("sections") or []:
        for tool in section.get("tools") or []:
            if not isinstance(tool, dict):
                continue
            name = str(tool.get("name") or "").strip()
            if not name or name == BLACKLIST_TOOL or name in seen:
                continue
            if _entry_level(tool) in levels:
                available.append(name)
                seen.add(name)
    blocked = 1 not in levels

    lines = ["【当前可用的工具】"]
    lines.append("、".join(sorted(available)))
    normal_hint, blocked_hint = blacklist_hints()
    lines.append(blocked_hint if blocked else normal_hint)

    # 「工具权限」页里按档填写的说明，按该用户实际可用的档位依次拼接
    notes = level_notes()
    if notes:
        # 通用注入（不分档位，所有用户都会收到）
        common = notes.get("ALL")
        if common:
            lines.append(common)
        # 不叠加：只注入该用户**最高可用档位**那一段
        top = max(levels) if levels else 0
        own = notes.get(f"L{top}")
        if own:
            lines.append(own)
    return "\n".join(lines)


LEGACY_MIGRATED_KEY = "tool_permission_migrated_from_misc"


def migrate_from_misc_settings() -> str:
    """把「杂项」里的 shell / 宿主机权限搬进 L3/L4 名单（只做一次）。

    旧语义：`*_restriction_enabled=False` = 不限制（所有人可用）→ 名单写 `*`；
            `True` = 只有名单里的人可用 → 直接把名单搬过来。
    """
    path = config.SYSTEM_SETTINGS_PATH
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except Exception:  # noqa: BLE001
        return ""
    if not isinstance(data, dict) or data.get(LEGACY_MIGRATED_KEY):
        return ""

    section = data.get(SETTINGS_KEY)
    section = dict(section) if isinstance(section, dict) else {}
    notes: list[str] = []

    shell_users = [str(u).strip() for u in (data.get("shell_allowed_users") or []) if str(u).strip()]
    shell_restricted = bool(data.get("shell_restriction_enabled", False))
    if not section.get("L3_whitelist_users"):
        section["L3_whitelist_users"] = shell_users if shell_restricted else [WILDCARD]
        notes.append("L3 来自 shell 名单" + ("（已放开为所有人）" if not shell_restricted else ""))

    mnt_users = [str(u).strip() for u in (data.get("mnt_write_allowed_users") or []) if str(u).strip()]
    mnt_restricted = bool(data.get("mnt_write_restriction_enabled", False))
    if not section.get("L4_whitelist_users"):
        section["L4_whitelist_users"] = mnt_users if mnt_restricted else [WILDCARD]
        notes.append("L4 来自宿主机写名单" + ("（已放开为所有人）" if not mnt_restricted else ""))

    if not notes:
        return ""

    data[SETTINGS_KEY] = section
    data[LEGACY_MIGRATED_KEY] = True
    try:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        debug_log(f"[permission] 迁移杂项设置失败: {exc}")
        return ""
    debug_log("[permission] 已把杂项里的权限设置迁到 tool_permission: " + "；".join(notes))
    return "；".join(notes)

"""`tool-blacklist` 工具：把用户加入操作黑名单（只能加入，移出由管理员操作）。

为什么放在 L0：被屏蔽的用户必须能自救，否则一个误配置就把人锁死。
作用域限制：**只能改自己**（参数里的用户 id 与调用者不一致时直接拒绝），
防止有人把别人拉黑。dashboard 侧的管理能力不受这条限制（特权通道）。

用法：
  工具调用:tool-blacklist|2195239690|      （加入黑名单；用户 id 留空表示自己）

**只允许加入，不提供移出**：移出只能由管理员在 dashboard「工具权限」页手动操作。
"""

from __future__ import annotations

import json
from pathlib import Path

from app import config
from app.logger import log

SETTINGS_KEY = "tool_permission"


def _settings_path() -> Path:
    return Path(config.SYSTEM_SETTINGS_PATH)


def _load() -> dict:
    path = _settings_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}


def _save(data: dict) -> str:
    path = _settings_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        return f"错误：写入 system_settings.json 失败（{exc}）"
    return ""


def _blacklist(data: dict) -> list[str]:
    section = data.get(SETTINGS_KEY)
    if not isinstance(section, dict):
        return []
    values = section.get("L1_blacklist_users") or []
    if not isinstance(values, list):
        return []
    return sorted({str(item).strip() for item in values if str(item).strip()})


def handle(args: list[str], kwargs: dict[str, str], caller: str) -> str:
    """入口：args[0] 目标用户（空=自己）、args[1] `-` 表示移除。"""
    target = str(
        (args[0] if args else "")
        or kwargs.get("user_id")
        or ""
    ).strip()
    action = str(args[1] if len(args) > 1 else kwargs.get("action") or "").strip()
    me = str(caller or "default").strip() or "default"
    target = target or me

    if target != me:
        return (
            "错误：只能把自己加入/移出黑名单"
            f"（调用者 {me}，目标是 {target}）。需要改别人请走 dashboard。"
        )

    data = _load()
    section = data.get(SETTINGS_KEY)
    if not isinstance(section, dict):
        section = {}
    current = _blacklist(data)
    if action in {"-", "remove", "移出", "解除"}:
        return "错误：本工具只能把用户加入黑名单；移出需要管理员手动操作"

    if me in current:
        return f"用户 {me} 已在黑名单内；如需恢复请联系管理员解除"
    current.append(me)
    section["L1_blacklist_users"] = sorted(current)
    data[SETTINGS_KEY] = section
    error = _save(data)
    if error:
        return error
    log(f"[permission] {me} 已把自己加入黑名单")
    return (
        f"已把用户 {me} 加入操作黑名单：文件写入、记忆写入、技能执行等工具将不可用；"
        "查询、读取、搜索这类操作不受影响。"
    )

"""运行环境能力（capability）：由部署程序写入，运行期只读。

设计口径（见 deploy/features.json）：
- 部署程序探测环境 + 用户选择功能 → 生成 `<DATA_DIR>/capability.json`；
- 本模块只负责**读**它，并提供能力查询；解析失败/文件缺失时退化为"全部开启"，
  保证既有部署（没有这个文件）行为完全不变；
- 环境变量可覆盖：`MYAGENT_CAP_<能力名>`（例如 `MYAGENT_CAP_browser=off`、`MYAGENT_CAP_search=builtin`）；
- 能力关闭的影响面：工具目录过滤（TOOL.json 的 groups 里用 `cap:<能力>` 标注）、
  提示词里的可用工具清单、dashboard「杂项 → 运行环境」展示。
"""

from __future__ import annotations

import json
import os
from typing import Any

from app import config
from app.logger import debug_log

# 能力名 → 关闭时的语义取值（True/False 或枚举字符串）
KNOWN_CAPABILITIES = {
    "tool_runtime": True,
    "memory_mode": "openviking",
    "openviking": True,
    "clawhub": True,
    "skill_exec": True,
    "browser": True,
    "search": True,
    "external_exec": True,
    "host_files": True,
    "image_gen": True,
    "fetch_extract": "full",
    "qq_adapter": True,
    "lan_expose": False,
    "storage": "auto",
}

# 视为"开启"的取值（其余一律当关闭）
_TRUTHY = {"on", "true", "yes", "1", "enabled", "full", "builtin", "local", "managed", "connect", "openviking"}
_FALSY = {"off", "false", "no", "0", "disabled", "none", "partial"}


def _path():
    return config.CAPABILITY_PATH


def _read() -> dict[str, Any]:
    path = _path()
    try:
        if not path.exists():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        debug_log(f"[capability] 读取 {path} 失败（按全部开启处理）: {exc}")
        return {}
    return data if isinstance(data, dict) else {}


def _features(data: dict[str, Any]) -> dict[str, Any]:
    features = data.get("features")
    return features if isinstance(features, dict) else {}


def raw(name: str):
    """能力原始取值：env 覆盖 > capability.json > 默认。"""
    env_key = f"MYAGENT_CAP_{name}"
    if env_key in os.environ:
        value = os.environ[env_key].strip()
        return _coerce(value)
    value = _features(_read()).get(name)
    if value is None:
        return KNOWN_CAPABILITIES.get(name)
    return _coerce(value)


def _coerce(value):
    if isinstance(value, bool):
        return value
    text = str(value).strip()
    lowered = text.lower()
    if lowered in _TRUTHY:
        return True if lowered in {"on", "true", "yes", "1", "enabled"} else text
    if lowered in _FALSY:
        return False
    return text


def enabled(name: str) -> bool:
    """能力是否可用（枚举型：off/none/partial 视为不可用）。"""
    value = raw(name)
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    return text not in _FALSY


def value(name: str):
    return raw(name)


def all_features() -> dict[str, Any]:
    """当前生效的能力表（含 env 覆盖），给 dashboard / 提示词展示用。"""
    return {name: raw(name) for name in KNOWN_CAPABILITIES}


def degraded() -> list[dict[str, str]]:
    """部署程序写入的降级清单（原样返回，供展示）。"""
    items = _read().get("degraded")
    if not isinstance(items, list):
        return []
    return [
        {
            "feature": str(item.get("feature") or ""),
            "reason": str(item.get("reason") or ""),
            # level=info 的是说明项（如"单体模式没 Docker"），不是降级
            "level": str(item.get("level") or "degrade"),
        }
        for item in items
        if isinstance(item, dict)
    ]


def tier() -> str:
    data = _read()
    return str(data.get("runtime_tier") or "unknown")


def summary() -> dict[str, Any]:
    data = _read()
    return {
        "tier": tier(),
        "os": str(data.get("os") or ""),
        "generated_at": str(data.get("generated_at") or ""),
        "features": all_features(),
        "degraded": degraded(),
        "source": str(_path()),
    }


def tool_allowed(capability_tokens: list[str]) -> bool:
    """工具的 `cap:` 标签是否全部满足（无标签 = 永远可见）。"""
    for token in capability_tokens:
        text = str(token).strip()
        if not text.lower().startswith("cap:"):
            continue
        name = text[4:].strip()
        if name and not enabled(name):
            return False
    return True

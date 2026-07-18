"""
agent_list.json 读取与用户级配置选择。

支持两种格式：

1. 旧格式，兼容原项目：
{
  "main": {...},
  "tool": {...},
  "reader": {...}
}

2. 新格式，支持按用户选择：
{
  "default": {
    "main": {...},
    "tool": {...},
    "reader": {...}
  },
  "users": {
    "dujiawei": {
      "main": {...},
      "tool": {...},
      "reader": {...}
    },
    "qq_123456": {
      "main": {
        "model_profile": "cheap-main"
      }
    }
  }
}

用户配置会覆盖 default：
- 如果用户不存在，使用 default。
- 如果用户存在但缺少某个 agent，使用 default 中的该 agent。
- 如果用户只覆盖某些字段，会在 default agent 配置基础上合并。
"""

import copy
import json
from pathlib import Path
from typing import Any


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if (
            key in result
            and isinstance(result[key], dict)
            and isinstance(value, dict)
        ):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _is_legacy_flat_config(data: dict[str, Any]) -> bool:
    return "default" not in data and "users" not in data


def load_agent_config(path: Path, user_id: str, agent_id: str) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    # 兼容原来的平铺格式：{"main": {...}, "tool": {...}}
    if _is_legacy_flat_config(data):
        if agent_id not in data:
            raise KeyError(f"未找到智能体配置：{agent_id}")
        return copy.deepcopy(data[agent_id])

    default_agents = data.get("default", {})
    users = data.get("users", {})

    # 也兼容 {"default": {"agents": {...}}, "users": {"u": {"agents": {...}}}}
    if "agents" in default_agents and isinstance(default_agents["agents"], dict):
        default_agents = default_agents["agents"]

    user_config = users.get(user_id) or users.get(str(user_id)) or {}
    if "agents" in user_config and isinstance(user_config["agents"], dict):
        user_config = user_config["agents"]

    default_agent_config = default_agents.get(agent_id)
    user_agent_config = user_config.get(agent_id)

    if default_agent_config is None and user_agent_config is None:
        raise KeyError(f"未找到智能体配置：user={user_id}, agent={agent_id}")

    if default_agent_config is None:
        return copy.deepcopy(user_agent_config)

    if user_agent_config is None:
        return copy.deepcopy(default_agent_config)

    return deep_merge(default_agent_config, user_agent_config)

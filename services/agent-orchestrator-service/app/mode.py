"""模式（mode）开关：同一个 agent、同一个会话，切换「注入哪套提示词 + 用哪个模型」。

与 `切换:` 的分工
----------------
- `切换:<agent>` 改的是**会话默认智能体**（`AgentRuntime.default_agent[session]`）：
  换 agent 就是换人，历史按发言者分开存（`{agent_id}_{session}`）。
- `模式:<name>` 改的是**同一 agent 下的模式**：`agent_id` 不变 → 会话 key 不变 →
  上下文天然共用，只有注入的提示词与模型切换。

与用户约定的语义
----------------
1. 缺省模式是 `default`，等于 agent 自己的配置（改造前后行为完全一致）；
2. 切到别的 agent 时（`切换:`），模式**重置为 `default`**；
3. 模式可以覆盖 `files` / `model_profile` / `temperature` / `max_tokens`，
   其余键沿用 agent 级配置（浅合并，模式优先）。

模式定义在 `agent_list.json` 的每个 agent 下：

    "main": {
      "model_profile": "default-main",
      "files": ["SOUL.md", "TOOL.md", "IDENTITY.md"],
      "modes": {
        "special": {
          "files": ["SOUL_special.md", "IDENTITY.md"],
          "model_profile": "default-talker",
          "temperature": 1.2
        }
      }
    }
"""

from __future__ import annotations

from pathlib import Path

DEFAULT_MODE = "default"

# 模式层**不**维护功能性文件名白名单（PROCESS / world_info / TOOL 变体）。
# 声明式提示词文件是否存在，由注入层 context_builder.build_system_prompt 统一判定；
# 这样加一个新的功能性文件/变体只需要改注入层一处，不会出现两个事实源。


def normalize_mode(name) -> str:
    """把任意输入归一成模式名；空/缺省 → `default`；保留字大小写不敏感。"""
    text = str(name or "").strip()
    if not text:
        return DEFAULT_MODE
    return DEFAULT_MODE if text.lower() == DEFAULT_MODE else text


def canonical_mode(agent_config: dict, name) -> str:
    """把用户/模型写的模式名对齐到配置里的规范写法（大小写不敏感）。

    - `default` / `Default` / `DEFAULT` → `default`
    - `SPECIAL` → 配置里定义的 `special`
    - 找不到对应项时原样返回（由 validate_mode 报"未知模式"）
    """
    text = normalize_mode(name)
    if text == DEFAULT_MODE:
        return DEFAULT_MODE
    modes = (agent_config or {}).get("modes")
    if not isinstance(modes, dict):
        return text
    if text in modes:
        return text
    lowered = text.lower()
    for key in modes:
        candidate = str(key or "").strip()
        if candidate.lower() == lowered:
            return candidate
    return text


def available_modes(agent_config: dict) -> list[str]:
    """可用模式清单（永远包含 default，排在第一个）。"""
    modes = (agent_config or {}).get("modes")
    names = [DEFAULT_MODE]
    if isinstance(modes, dict):
        for name in modes:
            text = str(name or "").strip()
            if text and text != DEFAULT_MODE and text not in names:
                names.append(text)
    return names


def mode_config(agent_config: dict, mode: str) -> dict:
    """取某个模式的配置块；default / 未知名 → 空字典。"""
    name = canonical_mode(agent_config, mode)
    if name == DEFAULT_MODE:
        return {}
    modes = (agent_config or {}).get("modes")
    if not isinstance(modes, dict):
        return {}
    block = modes.get(name)
    return dict(block) if isinstance(block, dict) else {}


def effective_config(agent_config: dict, mode: str) -> dict:
    """agent 配置叠加模式覆盖（模式优先；default 时原样返回）。"""
    base = dict(agent_config or {})
    override = mode_config(base, mode)
    if not override:
        return base
    merged = {**base, **override}
    # modes 自身不参与合并结果的下游判断，但保留原样没坏处
    return merged


def validate_mode(agent_config: dict, prompt_dir: Path, mode: str) -> str:
    """校验模式是否可用；可用返回空串，否则返回给用户看的错误文本。

    - 未定义的模式名 → 只报「未知模式」（不列出可用模式：信息隐藏）
    - 不校验 files 是否存在：那由注入层（prompt 构建）负责，避免两处事实源
    """
    name = canonical_mode(agent_config, mode)
    if name == DEFAULT_MODE:
        return ""

    modes = (agent_config or {}).get("modes")
    if not isinstance(modes, dict) or name not in modes:
        # 信息隐藏：不列出 available_modes（具体有哪些模式只留在代码/配置里）
        return f"未知模式 {name!r}"

    block = modes.get(name)
    if not isinstance(block, dict):
        return f"模式 {name!r} 的定义不是对象，无法使用"

    # 只校验"模式是否定义、形态是否合法"。
    # files 里声明的提示词是否存在，由注入层统一判定（唯一事实源，见模块头注释）。
    return ""

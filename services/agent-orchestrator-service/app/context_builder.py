"""上下文拼接（context assembly）。

从 `agent_runtime.py` 抽出的「读配置 → 拼 system prompt → 拼 messages」部分，
集中在这里维护，agent_runtime 只负责模型调用、响应解析、工具与智能体路由。

负责：
1. system prompt 构建（`build_system_prompt`）
   - GLOBAL_SETTING.md（全局）
   - agent_list.json 的 `files` 声明逐个读取
   - PROCESS 变体（process.md / process_turn.md / process_clock.md）→ 结构化存储注入
   - world_info.md → 声明式开关（内容作为两阶段阶段1 的职责变更指令）
   - TOOL.md → 选项块 + 全局 TOOL.json 工具目录注入（见下）
2. send() 的 messages 组装（`build_send_context`）
   - 系统提示 / 长上下文 / 任务记忆 / 用户输入 / vars / 本轮输入（+ 世界书注入位）
3. 世界书注入（概念词提取、命中去重、单次与两阶段调用）

不负责：模型档案与采样参数选择、响应解析、工具执行与智能体路由。

TOOL 注入
---------
工具清单不再由每个智能体手写 `TOOL.md`，而是统一维护在全局
`<SYSTEM_PROMPT_DIR>/TOOL.json`（dashboard「系统提示词 → 全局文件」可直接编辑）：

```
{
  "version": 1,
  "title": "# 工具及技能调用设置",
  "sections": [
    {"id": "调用格式", "title": "## 调用格式", "text": "当需要调用工具时…"},
    {"id": "内置工具", "title": "## 内置工具", "tools": [
      {"name": "shell", "args": "命令", "desc": "执行 shell 命令"},
      {"name": "fetch", "args": "url|method|data", "desc": "发送 HTTP 请求",
       "notes": ["不确定页面结构 → data 留空"], "groups": ["网络"]}
    ]}
  ]
}
```

`TOOL.md` 只保留「注入选项」，正文可选（作为本智能体的补充说明追加）：

    ```tool-inject
    {"sections": ["*"], "tools": {"include": ["*"], "exclude": ["codex"]}}
    ```

- `sections`：段 id 列表（`["*"]` 全部，`"!段id"` 表示排除），也支持 `{"include": [], "exclude": []}`
- `tools`：`{"include": ["*"], "exclude": []}`，token 可写工具名 / 段 id / 工具 `groups` 标签
- `show_title`：是否输出目录根标题（默认 true）
- `body_position`：正文放注入内容之后（默认 `after`）还是之前（`before`）
- `enabled`：false 表示该智能体完全不注入工具段

目录侧：段或工具写 `"enabled": false` 即「默认不注入」，只有在选项里被点名（写全 id / 工具名）
才会注入 —— 用来放 process / worldinfo 这类只对部分智能体开放的指令。

兼容策略：`TOOL.md` 里没有 `tool-inject` 选项块（老式手写文件）或选项块 JSON 非法
→ 整份原文照旧注入，行为与改造前一致。

提示词引用与占位符
------------------
除"读文件原文注入"外，每个提示词文件（`GLOBAL_SETTING.md` / 各 agent 的 `.md` / `world_info.md` /
`TOOL.md`）的内容还支持两种间接形式，实现在 `app/prompt_refs.py`：

- `@ref=<agent_id>`：**读取期**把整份内容替换成「另一个智能体的同名文件」（只允许一层；
  目标缺失 / 非法 / 目标内部也有 `@ref=` 一律抛 `PromptRefError`，不静默降级）
- `{agent_id}`：**拼接完成后**统一替换为当前请求方的 agent id（白名单键，其余花括号原样保留）

两者放在这一层的原因是不变量：`messages` 里的文本必须与 `soul_content`、`world_info_override`
这两个"记录串"完全一致（阶段1 靠 `==` 剔除 SOUL），所以替换一次做完，不让下游看到模板原文。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from app import config
from app import prompt_refs
from app.logger import debug_log

# PROCESS 长期事件记录：文件名即模式声明
# （PROCESS.md=不报时 / PROCESS_turn.md=轮次计数 / PROCESS_clock.md=时钟）
_PROCESS_FILE_NAMES = {"process.md", "process_turn.md", "process_clock.md"}

# 世界书（World Info）两阶段：files 含 world_info.md 即声明启用
# （虚假文件，不读取内容，与 PROCESS 同款机制）
_WORLD_INFO_FILE_NAMES = {"world_info.md"}

# TOOL 声明：tool.md = 选项块在自身 / tool.json = 直接声明注入全局目录全量
_TOOL_FILE_NAMES = {"tool.md", "tool.json"}

# 全局工具目录文件名（位于 SYSTEM_PROMPT_DIR 根目录，与 GLOBAL_SETTING.md 同级）
TOOL_CATALOG_FILENAME = "TOOL.json"
# TOOL.md 里选项块的围栏标签
TOOL_OPTION_BLOCK_TAG = "tool-inject"

# 阶段1（列概念词）的职责变更指令，两阶段世界书用
_WORLD_INFO_PHASE1_INSTRUCTION = (
    "这是一个准备步骤：禁止输出回复正文，禁止角色扮演，禁止输出任何协议指令。\n"
    "无论消息是询问、陈述、动作还是闲聊，你的唯一输出必须是概念词清单。\n"
    "先分析消息中涉及的关键名词/概念（地名、人物、组织、物品、专属名词等），再列出。\n"
    "格式：第一行必须是【概念词】词1、词2、词3\n"
    "若确认本次回复不需要任何世界概念，输出：【概念词】无\n"
    "系统将根据这些词注入相关世界设定，确保你的回复符合世界设定。"
)

_OPTION_BLOCK_RE = re.compile(
    r"```[ \t]*" + TOOL_OPTION_BLOCK_TAG + r"[ \t]*\r?\n(.*?)```",
    re.DOTALL | re.IGNORECASE,
)


# ─── 通用小工具 ──────────────────────────────────────────────


def process_mode_from_files(files) -> str:
    """取 files 中第一个 PROCESS 变体对应的模式：turn / clock / none / ""（未配置）。"""
    for filename in files or []:
        stem = str(filename).strip().lower()
        if stem in _PROCESS_FILE_NAMES:
            if "turn" in stem:
                return "turn"
            if "clock" in stem:
                return "clock"
            return "none"
    return ""


def world_info_mode_from_files(files) -> bool:
    """files 含 world_info.md → 启用两阶段世界书（否则完全跳过世界书）。"""
    for filename in files or []:
        if str(filename).strip().lower() in _WORLD_INFO_FILE_NAMES:
            return True
    return False


def safe_process_segment(value: str, default: str = "default") -> str:
    """user_id / agent_id 转安全目录名，避免路径逃逸。"""
    raw = str(value or default).strip() or default
    safe = re.sub(r"[^0-9A-Za-z_.@-]+", "_", raw).strip("._") or default
    return "default" if safe in {".", ".."} else safe


def atomic_write_text(path: Path, text: str) -> None:
    """原子写：先写 .tmp 再 replace，避免并发读到半截文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def load_system_settings() -> dict:
    """读取 dashboard「杂项」写入的 system_settings.json，失败返回空字典。"""
    try:
        path = config.SYSTEM_SETTINGS_PATH
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def image_receive_enabled() -> bool:
    """图像接收开关，默认开启。"""
    return bool(load_system_settings().get("image_receive_enabled", True))


def identity_read_enabled() -> bool:
    """main 是否读取 IDENTITY.md，默认开启。"""
    return bool(load_system_settings().get("main_read_identity", True))


def _resolve_prompt_file(prompt_dir: Path, filename: str) -> Path | None:
    """在智能体提示词目录里按文件名找文件（大小写不敏感，磁盘上可能是 TOOL.md）。"""
    return prompt_refs.resolve_named_file(prompt_dir, filename)


def _read_prompt_text(path: Path, *, agent_id: str, filename: str) -> str:
    """读一个提示词文件，并在读取期解析 `@ref=` 跳转（整文件替换为另一 agent 的同名文件）。

    解析失败抛 `prompt_refs.PromptRefError`（不静默降级）。
    """
    raw = path.read_text(encoding="utf-8")
    return prompt_refs.resolve_text(
        raw,
        agent_id=agent_id,
        filename=filename,
        system_prompt_dir=config.SYSTEM_PROMPT_DIR,
    )


def _assert_process_declarative(prompt_dir: Path, filename: str, *, agent_id: str) -> None:
    """PROCESS 声明文件的内容根本不被读取：写了 @ref= 无意义，显式报错。"""
    path = _resolve_prompt_file(prompt_dir, filename)
    if path is None:
        return
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return
    if prompt_refs.has_ref_directive(text):
        raise prompt_refs.PromptRefError(
            "PROCESS 声明文件（process*.md）的内容不会被读取，@ref= 不生效；"
            "如需共享请改其他文件",
            agent_id=agent_id,
            filename=filename,
            line=prompt_refs.first_ref_line(text) or "",
        )


# ─── TOOL.json 工具目录 ──────────────────────────────────────


def load_tool_catalog(path: Path | None = None) -> dict:
    """读取全局工具目录 TOOL.json；缺失 / 损坏返回 {}（调用方降级）。"""
    target = path or (config.SYSTEM_PROMPT_DIR / TOOL_CATALOG_FILENAME)
    try:
        if not target.exists():
            return {}
        data = json.loads(target.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
        debug_log(f"[tool] {target} 顶层不是对象，按缺失处理")
    except Exception as exc:
        debug_log(f"[tool] 读取工具目录失败 {target}: {exc}")
    return {}


def parse_tool_options(raw: str) -> tuple[dict | None, str]:
    """拆出 TOOL.md 里的 tool-inject 选项块与正文。

    返回 `(options, body)`；找不到选项块或 JSON 非法时返回 `(None, 原文.strip())`，
    由调用方按老式手写 TOOL.md 原文注入（向后兼容）。
    """
    text = raw or ""
    match = _OPTION_BLOCK_RE.search(text)
    if not match:
        return None, text.strip()
    try:
        options = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        debug_log(f"[tool] TOOL.md 选项块 JSON 非法，按原文注入: {exc}")
        return None, text.strip()
    if not isinstance(options, dict):
        debug_log("[tool] TOOL.md 选项块顶层必须是对象，按原文注入")
        return None, text.strip()
    body = (text[: match.start()] + text[match.end():]).strip()
    return options, body


def _normalize_selector(value, default_all: bool = True) -> tuple[set[str], set[str]]:
    """把段 / 工具选择器统一成 (include, exclude) 两组小写 token。

    支持：`None`、`"*"`、`["a", "!b"]`、`{"include": [...], "exclude": [...]}`。
    """
    include: set[str] = set()
    exclude: set[str] = set()

    def _add_include(token):
        token = str(token or "").strip().lower()
        if token:
            include.add(token)

    def _add_exclude(token):
        token = str(token or "").strip().lower()
        if token:
            exclude.add(token)

    if isinstance(value, dict):
        raw_include = value.get("include")
        if isinstance(raw_include, (list, tuple, set)):
            for item in raw_include:
                _add_include(item)
        elif raw_include is not None:
            _add_include(raw_include)
        raw_exclude = value.get("exclude")
        if isinstance(raw_exclude, (list, tuple, set)):
            for item in raw_exclude:
                _add_exclude(item)
        elif raw_exclude is not None:
            _add_exclude(raw_exclude)
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            token = str(item or "").strip()
            if token.startswith("!"):
                _add_exclude(token[1:])
            else:
                _add_include(token)
    elif value is not None:
        _add_include(value)

    if not include and default_all:
        include = {"*"}
    return include, exclude


def _selected(name, groups, include: set[str], exclude: set[str]) -> bool:
    """token 命中工具名 / 段 id / 工具 groups 标签即算命中。"""
    tokens = {str(name or "").strip().lower()}
    for group in groups or []:
        tokens.add(str(group or "").strip().lower())
    tokens.discard("")
    if "*" in exclude or (tokens & exclude):
        return False
    return "*" in include or bool(tokens & include)


def _explicitly_named(include: set[str], token: str) -> bool:
    """include 里是否直接点名（`*` 和 groups 标签都不算）。

    用于 `"enabled": false` 的段 / 工具：默认不注入，只有被点名才注入。
    """
    token = str(token or "").strip().lower()
    return bool(token) and token in include


def render_tool_line(tool: dict) -> str:
    """单条工具渲染成 `- 名字|参数 - 说明`（notes 作为缩进子条）。"""
    name = str(tool.get("name") or "").strip()
    if not name:
        return ""
    args = str(tool.get("args") or "").strip()
    desc = str(tool.get("desc") or "").strip()
    head = f"- {name}" + (f"|{args}" if args else "")
    if desc:
        head += f" - {desc}"
    lines = [head]
    for note in tool.get("notes") or []:
        note = str(note or "").strip()
        if note:
            lines.append(f"  - {note}")
    return "\n".join(lines)


def _render_section(
    section: dict,
    section_id: str,
    include_tools: set[str],
    exclude_tools: set[str],
) -> str:
    """渲染单个段：有 text 用 text，否则按工具选择器渲染工具条目。"""
    title = str(section.get("title") or "").strip()
    text = str(section.get("text") or "").strip()
    if text:
        return f"{title}\n{text}" if title else text

    lines = []
    for tool in section.get("tools") or []:
        if not isinstance(tool, dict):
            continue
        tool_name = str(tool.get("name") or "").strip()
        # 段内 enabled=false 的工具：默认不注入，被选项点名才注入
        if tool.get("enabled", True) is False and not _explicitly_named(include_tools, tool_name):
            continue
        if not _selected(tool_name, tool.get("groups"), include_tools, exclude_tools):
            continue
        line = render_tool_line(tool)
        if line:
            lines.append(line)
    if not lines:
        # 该段工具全被筛掉：连标题一起省略，避免留下空段
        return ""
    body = "\n".join(lines)
    return f"{title}\n\n{body}" if title else body


def render_tool_catalog(catalog: dict, options: dict | None = None) -> str:
    """按选项把全局工具目录渲染成待注入文本。"""
    options = options or {}
    include_sections, exclude_sections = _normalize_selector(options.get("sections"))
    include_tools, exclude_tools = _normalize_selector(options.get("tools"))

    blocks: list[str] = []
    if options.get("show_title", True):
        title = str(catalog.get("title") or "").strip()
        if title:
            blocks.append(title)

    for section in catalog.get("sections") or []:
        if not isinstance(section, dict):
            continue
        section_id = str(section.get("id") or "").strip()
        # 目录里 enabled=false 的段：默认不注入，被选项点名才注入
        if section.get("enabled", True) is False and not _explicitly_named(include_sections, section_id):
            continue
        if not _selected(section_id, [], include_sections, exclude_sections):
            continue
        rendered = _render_section(section, section_id, include_tools, exclude_tools)
        if rendered:
            blocks.append(rendered)
    return "\n\n".join(blocks)


def build_tool_injection(prompt_dir: Path, declared_name: str, agent_id: str = "") -> str:
    """构建某智能体的 TOOL 注入文本；无内容返回 ""。

    - files 里写 `TOOL.md`：读该文件（支持 `@ref=` 跳到另一 agent 的同名 TOOL.md），
      有 tool-inject 选项块则按选项注入全局目录，没有选项块（老式手写文件）则原文注入。
    - files 里写 `TOOL.json`：直接注入全局目录全量（无 per-agent 选项）。
    """
    catalog = load_tool_catalog()
    name = str(declared_name or "").strip().lower()

    if name == TOOL_CATALOG_FILENAME.lower():
        options: dict = {"sections": ["*"], "tools": {"include": ["*"]}}
        body = ""
    else:
        path = _resolve_prompt_file(prompt_dir, "tool.md")
        if path is None:
            return ""
        try:
            # 读取期解析 @ref=（跳转目标由声明的文件名决定：同名替换）
            raw = _read_prompt_text(path, agent_id=agent_id, filename=declared_name)
        except prompt_refs.PromptRefError:
            raise
        except Exception as exc:
            debug_log(f"[tool] 读取 {path} 失败: {exc}")
            return ""
        options, body = parse_tool_options(raw)
        if options is None:
            # 老式手写 TOOL.md：整份原文注入，行为与改造前一致
            return raw.strip()

    if options.get("enabled", True) is False:
        return ""

    if not catalog:
        debug_log("[tool] 全局 TOOL.json 缺失或为空，仅注入 TOOL.md 正文")
        return body

    text = render_tool_catalog(catalog, options)
    if body:
        if str(options.get("body_position") or "after").strip().lower() == "before":
            text = f"{body}\n\n{text}" if text else body
        else:
            text = f"{text}\n\n{body}" if text else body
    return text.strip()


# ─── 拼接结果结构 ────────────────────────────────────────────


@dataclass
class SystemPromptParts:
    """system prompt 构建结果（由 AgentRuntime 存到实例上）。"""

    messages: list[dict] = field(default_factory=list)
    identity_indexes: list[int] = field(default_factory=list)
    soul_content: str = ""
    world_info_override: str = ""


@dataclass
class ContextBundle:
    """一次 send() 需要的全部 messages 片段与调用模式。"""

    messages: list[dict] = field(default_factory=list)
    base_head: list[dict] = field(default_factory=list)
    base_tail: list[dict] = field(default_factory=list)
    image_count: int = 0
    two_phase_world_info: bool = False


class ContextBuilder:
    """按 agent 实例构建上下文；只读 agent 的配置与存储，不改任务主流程。"""

    def __init__(self, agent):
        self._agent = agent

    # ── 便捷读取 ────────────────────────────────────────────

    @property
    def _config(self) -> dict:
        return self._agent.config or {}

    @property
    def _files(self) -> list:
        return list(self._config.get("files", []) or [])

    # ── system prompt 构建 ─────────────────────────────────

    def build_system_prompt(self) -> SystemPromptParts:
        """构建 system prompt：GLOBAL_SETTING.md + files 声明的各文件 / 结构化段。

        两处"内容不是文件原文"的处理：
        - `@ref=` 跳转在**读取期**逐文件解析（整文件替换为另一 agent 的同名文件，见 prompt_refs）
        - `{agent_id}` 占位符在**拼接完成后**统一替换（简单规则：所有提示词段 + 记录串）
        """
        agent = self._agent
        prompt_dir = config.SYSTEM_PROMPT_DIR / agent.id
        global_setting = config.SYSTEM_PROMPT_DIR / "GLOBAL_SETTING.md"

        parts = SystemPromptParts()

        if global_setting.exists():
            content = _read_prompt_text(
                global_setting, agent_id=agent.id, filename="GLOBAL_SETTING.md"
            ).strip()
            if content:
                parts.messages.append({"role": "system", "content": content})

        for filename in self._config.get("files", []):
            key = str(filename).strip().lower()

            if key in _PROCESS_FILE_NAMES:
                # PROCESS 变体不读 md：内容由结构化存储注入（见 build_process_message）
                _assert_process_declarative(prompt_dir, filename, agent_id=agent.id)
                continue

            if key in _WORLD_INFO_FILE_NAMES:
                # world_info.md 普通轮仍为声明式开关（不注入内容）；
                # 其内容作为两阶段阶段1 的「职责变更指令」，取代 SOUL.md 注入位置。
                wi_path = _resolve_prompt_file(prompt_dir, "world_info.md")
                if wi_path is not None:
                    parts.world_info_override = _read_prompt_text(
                        wi_path, agent_id=agent.id, filename=filename
                    ).strip()
                continue

            if key in _TOOL_FILE_NAMES:
                # TOOL：选项写在 TOOL.md，清单统一来自全局 TOOL.json
                tool_content = build_tool_injection(prompt_dir, filename, agent_id=agent.id)
                if tool_content:
                    parts.messages.append({"role": "system", "content": tool_content})
                continue

            file_path = prompt_dir / filename
            if not file_path.exists():
                continue
            content = _read_prompt_text(file_path, agent_id=agent.id, filename=filename).strip()
            if content:
                parts.messages.append({"role": "system", "content": content})
                # 记录 main 的 IDENTITY.md 位置，供杂项开关在调用模型前拦截
                if agent.id == "main" and str(filename).lower() == "identity.md":
                    parts.identity_indexes.append(len(parts.messages) - 1)
                # 记录 SOUL.md 原文，两阶段阶段1 构建时剔除（避免角色代入干扰关键词提取）
                if str(filename).lower() == "soul.md":
                    parts.soul_content = content

        # 占位符替换：messages 与两个"内容被记下来"的串必须一起替换，
        # 否则阶段1 的 `m["content"] == soul_content` 判定会失效（SOUL 残留进阶段1）。
        for message in parts.messages:
            message["content"] = prompt_refs.substitute_placeholders(
                message["content"], agent_id=agent.id
            )
        parts.soul_content = prompt_refs.substitute_placeholders(
            parts.soul_content, agent_id=agent.id
        )
        parts.world_info_override = prompt_refs.substitute_placeholders(
            parts.world_info_override, agent_id=agent.id
        )

        return parts

    def effective_system_prompt(self) -> list[dict[str, str]]:
        """按杂项开关过滤系统提示词：main 关闭 IDENTITY.md 时跳过对应消息。"""
        agent = self._agent
        if agent.id != "main" or not agent._identity_system_indexes:
            return agent.system_prompt
        if identity_read_enabled():
            return agent.system_prompt
        blocked = set(agent._identity_system_indexes)
        return [m for i, m in enumerate(agent.system_prompt) if i not in blocked]

    # ── PROCESS / vars 结构化段 ─────────────────────────────

    def build_process_message(self, count_turn: bool = True) -> dict | None:
        """按 config files 里的 PROCESS 变体，从结构化存储构造 PROCESS system 消息。

        - turn 模式：count_turn=True（用户请求首次 send）时 turn+1 并落盘，
          注入「现在时间是 第N轮对话」；工具回执/智能体转交等内部回调不计数
        - clock / none 模式：不报时（框架请求自带时间戳）
        - 无条目时返回 None（不注入空段）
        """
        agent = self._agent
        mode = process_mode_from_files(self._config.get("files", []))
        if not mode:
            return None

        user_seg = safe_process_segment(agent.user_id)
        agent_seg = safe_process_segment(agent.id)
        path = config.PROCESS_DIR / user_seg / f"{agent_seg}.json"

        store: dict = {}
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    store = loaded
            except Exception:
                store = {}
        else:
            # 配置了 PROCESS 但存储不存在：自动创建空白文件
            store = {"items": []}
            atomic_write_text(path, json.dumps(store, ensure_ascii=False, indent=2))

        items = store.get("items")
        if not isinstance(items, list):
            items = []
            store["items"] = items

        header = "以下是你的长期事件记录（PROCESS）"
        if mode == "turn":
            try:
                turn = int(store.get("turn") or 0)
            except (TypeError, ValueError):
                turn = 0
            if count_turn:
                turn += 1
                store["turn"] = turn
                atomic_write_text(path, json.dumps(store, ensure_ascii=False, indent=2))
            header += f"，现在时间是 第{turn}轮对话"
        header += "，请参考："

        if not items:
            return None

        lines = [header]
        for index, item in enumerate(items, 1):
            if isinstance(item, dict):
                title = str(item.get("title") or "").strip()
                content = str(item.get("content") or "").strip()
                entry = f"{title}：{content}" if title else content
            else:
                entry = str(item)
            lines.append(f"{index}. {entry}")
        return {"role": "system", "content": "\n".join(lines)}

    def build_vars_message(self) -> dict | None:
        """从 PROCESS 存储读取 vars 数值段（与事件段分离，独立注入）。

        - 只显示存储 key=value 平铺（aliases 别名映射不注入，避免模型看到别名）
        - 插入位置：用户对话前、世界书后（由调用方决定）
        - 无 vars 或无 PROCESS 文件 → 返回 None（不注入空段）
        """
        agent = self._agent
        user_seg = safe_process_segment(agent.user_id)
        agent_seg = safe_process_segment(agent.id)
        path = config.PROCESS_DIR / user_seg / f"{agent_seg}.json"
        if not path.exists():
            return None
        try:
            store = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        if not isinstance(store, dict):
            return None
        vars_map = store.get("vars")
        if not isinstance(vars_map, dict) or not vars_map:
            return None
        lines = ["【当前状态变量】"]
        for vkey, vval in vars_map.items():
            lines.append(f"- {vkey} = {vval}")
        return {"role": "system", "content": "\n".join(lines)}

    # ── 世界书 ─────────────────────────────────────────────

    def world_info_enabled(self) -> bool:
        """files 含 world_info.md → 启用两阶段世界书（否则完全跳过）。"""
        return world_info_mode_from_files(self._config.get("files", []))

    def extract_concepts(self, thinking: str) -> list[str]:
        """从阶段1输出中提取【概念词】清单；无清单返回 []。"""
        if not thinking:
            return []
        m = re.search(r"【概念词】\s*(.+)", thinking)
        if not m:
            m = re.search(r"概念词\s*[:：]\s*(.+)", thinking)
        if not m:
            return []
        line = m.group(1).strip().rstrip("。.！!？?")
        if line in ("无", "无概念词", "无。", "none", "None", "NONE"):
            return []
        words = []
        for part in re.split(r"[、，,;；/|\\\s]+", line):
            w = part.strip()
            if w and w not in words:
                words.append(w)
        return words

    def format_world_info_block(self, hits) -> str:
        """命中条目拼成【世界设定】注入段。"""
        lines = ["【世界设定】"]
        for hit in hits:
            keys = "、".join(hit.get("keys") or [])
            tag = f"[{keys}]" if keys else ""
            lines.append(f"- {tag} {hit.get('content', '')}".strip())
        return "\n".join(lines)

    def dedup_identity_hits(self, hits, head) -> list:
        """世界书注入去重：条目内容已存在于该智能体自身常驻内容（identity 等）中则跳过。

        解决"某词条已常驻在 A 的 identity.md，同时又作为世界书给群组里其他智能体"的场景：
        词条 scope 照常填群组，A 命中后检测到内容重复自动跳过，无需对 A 单独做例外。
        规则对所有智能体统一生效（谁的常驻内容里已有就跳过谁的）。
        """
        if not hits or not head:
            return hits
        head_text = re.sub(r"\s+", "", " ".join(
            str(m.get("content") or "") for m in head if isinstance(m, dict)
        ))
        if not head_text:
            return hits
        kept = []
        for hit in hits:
            content = str(hit.get("content") or "").strip()
            if not content:
                kept.append(hit)
                continue
            norm = re.sub(r"\s+", "", content)
            if norm and norm in head_text:
                debug_log(f"[{self._agent.user_id}] world_info 跳过重复条目（内容已在自身常驻提示中）: {norm[:30]}")
                continue
            kept.append(hit)
        return kept

    def search_world_info_hits(self, task, user_query: str, trigger_sources) -> list:
        """搜索世界书命中；失败静默降级返回 []。"""
        agent = self._agent
        try:
            return agent.context_client.search_world_info(
                user_id=task.user.id,
                agent_id=agent.id,
                query=user_query or "",
                recent_messages=trigger_sources or [],
                max_tokens=int(self._config.get("world_info_max_tokens", 0) or 0)
                or getattr(config, "WORLD_INFO_MAX_TOKENS", 1500),
                max_entries=int(self._config.get("world_info_max_entries", 0) or 0)
                or getattr(config, "WORLD_INFO_MAX_ENTRIES", 20),
            )
        except Exception as exc:
            debug_log(f"[{agent.user_id}] world_info 匹配失败: {exc}")
            return []

    def single_call_with_hits(self, task, head, tail, model_profile, params, hits):
        """单次调用（可带世界书注入）；失败返回 None。"""
        agent = self._agent
        hits = self.dedup_identity_hits(hits, head)
        if hits:
            tail = [{"role": "system", "content": self.format_world_info_block(hits)}] + list(tail)
        try:
            return agent.model_client.chat_completion(
                task_id=task.task_id,
                agent_id=agent.id,
                model_profile=model_profile,
                messages=head + tail,
                params=params,
            )
        except Exception as exc:
            debug_log(f"[{agent.user_id}] world_info 降级单次失败: {exc}")
            return None

    def world_info_two_phase_call(self, task, head, tail, model_profile, params, user_query: str):
        """两阶段世界书：阶段1 让模型列概念词 → 扫描匹配世界书 → 阶段2 注入后生成。

        - 阶段2 messages = head + 【世界设定·命中】 + tail（注入段插在 current_input 上方）
        - 阶段1 的思考过程不注入（已通过关键词拿到命中，思考全文塞回纯属浪费 token）
        - 阶段1 失败 / 无输出 → 降级单次调用，但**用户消息关键词仍触发世界书**（不浪费 query）
        - 阶段2 失败 → 返回 None（调用方降级为纯单次调用）
        - 触发源 = 【概念词】清单 ∪ 阶段1 输出全文（模型直接生成闲聊时也扫描）∪ 用户消息
        - 阶段1 head 剔除 SOUL.md（避免角色代入干扰列词），若 world_info.md 写了
          「职责变更指令」则取代 SOUL 位置注入；阶段2 恢复完整 head（含 SOUL，角色扮演）
        """
        agent = self._agent
        # 阶段1：职责变更 —— 剔除 SOUL.md；world_info.md 内容（职责变更指令）置于其位置
        soul_content = getattr(agent, "_soul_message_content", "") or ""
        wi_override = getattr(agent, "_world_info_override_content", "") or ""
        phase1_head: list[dict[str, str]] = []
        if wi_override:
            phase1_head.append({"role": "system", "content": wi_override})
        if soul_content:
            phase1_head.extend(
                m for m in head
                if not (isinstance(m, dict) and m.get("content") == soul_content)
            )
        else:
            phase1_head.extend(head)

        phase1_messages = phase1_head + tail + [{
            "role": "system",
            "content": _WORLD_INFO_PHASE1_INSTRUCTION,
        }]
        try:
            phase1_resp = agent.model_client.chat_completion(
                task_id=task.task_id,
                agent_id=agent.id,
                model_profile=model_profile,
                messages=phase1_messages,
                params=params,
            )
        except Exception as exc:
            debug_log(f"[{agent.user_id}] world_info 阶段1失败，降级单次（用户消息仍触发）: {exc}")
            phase1_resp = None

        thinking = (phase1_resp.get("reasoning") or "").strip() if phase1_resp else ""
        phase1_text = (phase1_resp.get("text") or "").strip() if phase1_resp else ""
        if phase1_resp is None or (not thinking and not phase1_text):
            # 阶段1无输出（模型不配合，如本地小模型）：降级单次调用，
            # 但用户消息里的关键词仍可触发世界书注入
            debug_log(f"[{agent.user_id}] world_info 阶段1无输出，降级单次（用户消息仍触发）")
            hits = self.search_world_info_hits(task, user_query, [])
            return self.single_call_with_hits(task, head, tail, model_profile, params, hits)

        # 触发源构建（v73 起按阶段1 输出质量分流）：
        # - 模型正确输出【概念词】行（词清单或"无"）→ 只用概念词清单 + 用户消息（query 已含）。
        #   此时 thinking 段巨大且基本是角色思考（噪音），不塞进触发源。
        # - 模型未遵守（输出回复正文等）→ thinking + 全文兜底（模型开演时 thinking 较短，
        #   扫描成本低，且能救回命中）。
        concepts = self.extract_concepts(phase1_text or thinking)
        has_concept_line = bool(re.search(r"【概念词】", phase1_text or ""))
        if has_concept_line:
            trigger_sources = list(concepts)
        else:
            trigger_sources = [thinking]
            if phase1_text and phase1_text != thinking:
                trigger_sources.append(phase1_text)
            trigger_sources.extend(concepts)
        hits = self.search_world_info_hits(task, user_query, trigger_sources)
        # 注入去重：内容已在该智能体自身常驻提示（identity 等）中的条目跳过
        hits = self.dedup_identity_hits(hits, head)

        # 阶段2：head + 【世界设定·命中】 + tail（无命中则不带注入段）
        if hits:
            tail = [{"role": "system", "content": self.format_world_info_block(hits)}] + list(tail)
        try:
            return agent.model_client.chat_completion(
                task_id=task.task_id,
                agent_id=agent.id,
                model_profile=model_profile,
                messages=head + tail,
                params=params,
            )
        except Exception as exc:
            debug_log(f"[{agent.user_id}] world_info 阶段2失败，降级单次: {exc}")
            return None

    # ── send() 组装 ────────────────────────────────────────

    def build_send_context(
        self,
        task,
        content: str,
        dialog_images: list | None = None,
        image_enabled: bool = True,
    ) -> ContextBundle:
        """组装一次 send() 要发给模型的全部片段。

        `content` / `dialog_images` 由调用方先 consume，避免重复消费任务临时输入；
        `image_enabled` = 杂项「图像接收」开关（由调用方读系统设置后传入）。
        """
        agent = self._agent
        dialog_images = list(dialog_images or [])

        # 外部群聊调度（agent→agent 轮转）：内容已在共享历史（grp_ session）里，
        # 抑制 user 输入重复注入，只提示"轮到该角色接话"（suppress_user_input 标记来自 metadata）
        if task.metadata.get("suppress_user_input"):
            current_input_messages = [
                {"role": "system", "content": "以下为本次群聊转场"},
                {"role": "user", "content": (
                    f"<{task.metadata.get('speaker_id') or getattr(task.caller, 'id', 'user')}>"
                    "（请基于共享对话历史继续接话，不要重复此前已输出的内容）"
                )},
            ]
        else:
            current_input_messages = [
                {"role": "system", "content": "以下为本次单轮对话内容"},
                {"role": "user", "content": f"<{getattr(task.caller, 'id', 'user')}>" + str(content)},
            ]
        if dialog_images:
            if agent.id == "main" and image_enabled:
                current_input_messages[1]["images"] = dialog_images
            elif agent.id == "main":
                current_input_messages[1]["content"] += (
                    f"\n\n（用户本次回复中附带了 {len(dialog_images)} 张图片，"
                    "但系统当前未开启图片接收，图片已被忽略。）"
                )

        long_context_message = [
            {"role": "system", "content": "以下是你和用户的历史对话记录，请根据上下文继续回答"}
        ] + agent.context_client.search_context(
            user_id=task.user.id,
            session_id=task.user.session_id,
            agent_id=agent.id,
            query=content,
            max_messages=int(self._config.get("max_messages", 6) or 6),
            max_tokens=int(self._config.get("context_max_tokens", 3000) or 3000),
            commit_limit=int(self._config.get("commit_limit", 0) or 0),
        )

        system_prompt_messages = self.effective_system_prompt()

        # 仅用户请求的首次 send() 计入轮次（caller 为 user 对象）；
        # 工具回执、智能体转交等内部回调 caller 是智能体，不计时。
        process_message = self.build_process_message(
            count_turn=bool(getattr(task, "caller", None) is getattr(task, "user", None))
        )
        if process_message:
            system_prompt_messages = system_prompt_messages + [process_message]

        task_memory_messages = []
        if agent.id == "main":
            task_memory = "以下是你在本次任务中的记忆:"
            for item in task.main_memory:
                task_memory += "\n" + item
            task_memory_messages = [{"role": "system", "content": task_memory}]

        user_input_messages = []
        if agent.id == "main":
            task_images = list(task.images or [])
            user_content = f"<{task.user.id}>" + task.content
            if task_images and image_enabled:
                user_input_messages = [
                    {"role": "system", "content": "以下为本次请求对话，请着重于下面部分\n下面是该任务用户原始请求"},
                    {"role": "user", "content": user_content, "images": task_images},
                ]
            elif task_images:
                user_input_messages = [
                    {"role": "system", "content": "以下为本次请求对话，请着重于下面部分\n下面是该任务用户原始请求"},
                    {"role": "user", "content": user_content + (
                        f"\n\n（用户本次发送了 {len(task_images)} 张图片，"
                        "但系统当前未开启图片接收，图片已被忽略。）"
                    )},
                ]
            else:
                user_input_messages = [
                    {"role": "system", "content": "以下为本次请求对话，请着重于下面部分\n下面是该任务用户原始请求"},
                    {"role": "user", "content": user_content},
                ]

        # 数值段（vars）插在用户对话前、世界书后：
        # - messages 组装时放 current_input 前面
        # - base_tail 同样前置 vars，世界书注入时 tail=[世界书块]+tail → 世界书→vars→用户对话
        vars_message = self.build_vars_message()
        vars_list = [vars_message] if vars_message else []

        messages = (
            system_prompt_messages
            + long_context_message
            + task_memory_messages
            + user_input_messages
            + vars_list
            + current_input_messages
        )
        # 世界书注入段插在 current_input（"以下为本次单轮对话内容"）上方：
        # base_head = 到 user_input 为止，base_tail = vars + current_input
        base_head = system_prompt_messages + long_context_message + task_memory_messages + user_input_messages
        base_tail = vars_list + current_input_messages

        image_count = len(dialog_images) + len(list(getattr(task, "images", None) or []))
        # 两阶段世界书：仅用户请求轮 + files 声明 world_info.md + 无图片（多模态轮保持单次）
        two_phase = (
            image_count == 0
            and self.world_info_enabled()
            and bool(getattr(task, "caller", None) is getattr(task, "user", None))
        )

        return ContextBundle(
            messages=messages,
            base_head=base_head,
            base_tail=base_tail,
            image_count=image_count,
            two_phase_world_info=two_phase,
        )

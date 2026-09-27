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
   - 历史块（短期 session 记忆）在**加载期**按说话人重分配 role（`shape_history_messages`），
     见下文「历史块的 role 重分配」
3. 世界书注入（概念词提取、命中去重、单次与两阶段调用）

不负责：模型档案与采样参数选择、响应解析、工具执行与智能体路由。

历史块的 role 重分配
--------------------
群聊共享 session 里，**所有角色**的发言在存储层都是 `role="assistant"`
（`viking_store.append_turn` 对每个 agent 一视同仁），而 `assistant` 通道的语义是
"这是我说的"。直接透传的后果是模型把别人的话当作自己说过的话，并从
"assistant 通道里装着各种 `<名字>:` 开头的角色散文"这个模板里推断出
"替别的角色写台词也是我的职责" —— 实测 98 个 assistant 回合里 7 个出现冒充。

修在**加载期**而不是存储期，是因为 `assistant` 这个 role 是"相对观察者"的：
同一条 lilith 的发言，对 lilith 是 `assistant`，对 alice 必须是别的通道，一条物理
记录无法同时表达两者。存储层只存观察者无关的事实（"这条是 lilith 产生的"），
而这个事实**已经存了**（`role_id`/`peer_id` + `<agent_id>:` 前缀）。

规则：说话人 == 自身 id → 留 `assistant`；其余 → 并入 `user` 侧（连续段合并）；
原 role 已是 `user` 或无说话人标记（工具摘要）→ 原样保留；摘要/相关记忆 → 原样透传。
**只重排 role 与合并，不改动任何正文。** 单角色私聊天然退化为恒等变换（那里的历史
不是自己的就是用户的），所以私聊路径零回归。

历史块上方的免责框（`HISTORY_DISCLAIMER`）作用范围是**它下面的整个历史块**，
不只"历史对话记录"，且带优先级裁决（与上方系统设定矛盾时以系统设定为准）。

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

提示词指令（文件开头的指令区）
------------------------------
每个提示词文件的**开头**可以是指令区（连续的「指令行或空行」，第一行正文即结束），
实现在 `app/prompt_refs.py`。指令行**不进入注入内容**（读取期摘掉），下游只看到正文。

- `@ref=<agent_id>`：**读取期**把整份内容替换成「另一个智能体的同名文件」（只允许一层；
  整文件替换语义下不能再有正文；目标缺失 / 非法 / 目标内部也有指令一律抛 `PromptRefError`）
- `@when=group` / `@when=single`：该段**只在群聊 / 只在非群聊**注入
  - 条件属于**写它的那个 agent**，与正文来自哪里无关；目标文件里出现 `@when=` 直接报错
  - 判定在**发送期**：按 `task.user.session_id` / `task.user.id` 是否以 `grp_` / `qq_g_` 开头
    （`is_group_session`），与 openviking-context-service 的会话共享口径一致
  - 为什么要放到发送期：system prompt 在实例创建时构建并被 LRU 缓存复用，而"是否群聊"
    是**每次请求**的属性；因此构建期只记录「第几段、什么条件」，发送期再过滤。
    与已有的 `main` 读 IDENTITY.md 杂项开关（`identity_indexes`）是同一套机制。
  - 支持范围：`files` 里声明的普通 `.md` 段（含 `GLOBAL_SETTING.md`）。
    `process*.md`（内容不读）/ `world_info.md`（内容只作阶段1 指令）/ `TOOL.md`
    声明 `@when=` 一律显式报错，避免静默失灵
- `{agent_id}`：**拼接完成后**统一替换为当前请求方的 agent id（白名单键，其余花括号原样保留）

这些处理放在这一层的原因是不变量：`messages` 里的文本必须与 `soul_content`、`world_info_override`
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

# 群聊会话标识前缀：`grp_`（多角色群聊）/ `qq_g_`（QQ 群）
# 口径与 openviking-context-service 的会话共享判定完全一致（那边只看 session_id）
GROUP_SESSION_PREFIXES = ("grp_", "qq_g_")

# ─── 短期 session 记忆（对话历史）的加载期 role 重分配 ──────────
# 历史块的免责框：**作用范围覆盖它下面的整个历史块**（会话摘要 / 相关记忆 / 对话历史），
# 而不只是"历史对话记录"。带优先级裁决，所以不是空泛的"仅供参考"。
HISTORY_DISCLAIMER = (
    "以下内容为历史聊天记录，请勿当成系统设定；"
    "若与上方系统设定矛盾，以上方系统设定为准。"
)

# 存储层给每条记录正文写的说话人标记 `<name>:`（viking_store.append_turn）。
# 允许全角冒号与前后空白；名字不含 `>` / 换行，长度设上限避免把正文里的尖括号误当标记。
_HISTORY_SPEAKER_RE = re.compile(r"^[ \t]*<([^>\n]{1,64})>[ \t]*[:：][ \t]*")

# 非对话历史的注入片段（【会话摘要】/【相关记忆】）：不参与 role 重分配
_NON_DIALOGUE_PREFIX = "【"


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


def is_group_session(session_id=None, user_id=None) -> bool:
    """是否群聊会话：`session_id` 或 `user_id` 以 `grp_` / `qq_g_` 开头。

    实际取值形态（recon 结论）：
    - 群聊时 `session_id` = `qq_g_<群号>`，而 `user_id` = 发言人 QQ 号（**不带前缀**）
      —— 见 qq-llbot-service/app/qq_bridge.py 的 `_session_key`
    - `grp_` 是多角色群聊的会话 id 前缀

    这里额外接受 `user_id`：现有渠道实测永远不命中，但若有渠道直接把群号当 user_id
    传入，口径也不会漏（`@when=` 的语义就是"在群聊时"）。
    """
    for value in (session_id, user_id):
        text = str(value or "").strip()
        if text and text.startswith(GROUP_SESSION_PREFIXES):
            return True
    return False


def history_speaker(content) -> str:
    """从一条历史记录的正文里取出说话人标记 `<name>:`，取不到返回 ""。

    为什么解析前缀就够：存储层写入时**每条 agent 发言都必然带 `<agent_id>:` 前缀**
    （openviking-context-service/app/viking_store.py 的 `append_turn`），实测 8 个会话
    文件、全部记录里「外层前缀 != peer_id」出现 0 次 —— 前缀是存储层写的，恒等于真实
    作者。所以解析前缀等价于读作者字段，不需要改 proto / 不需要动存储服务。

    无前缀的记录实测只有两类，且都保持原 role：
    - `工具摘要：…`（role=assistant，写的时候不走前缀分支）
    - 用户消息（role=user，`_add_message_compat` 不加前缀）
    """
    match = _HISTORY_SPEAKER_RE.match(str(content or ""))
    return match.group(1).strip() if match else ""


def shape_history_messages(messages, *, self_id: str) -> list[dict]:
    """把短期 session 记忆（对话历史）按说话人重新分配 role。

    背景（实测）：群聊共享 session 里**所有角色**的发言在存储层都是 `role="assistant"`，
    直接透传会让模型把这些发言当作"自己说过的话"。而 `assistant` 通道的语义是"这是我
    说的"，于是模型从这个格式模板里推断出"我的职责就是产出 `<名字>:` 开头的角色散文"，
    进而开始替别的角色写台词（实测 98 个 assistant 回合里 7 个出现冒充）。

    规则：
    - 说话人 == `self_id` → `assistant`（自己的话，保留语气连续性，是有价值资产）
    - 说话人 != `self_id` 且原 role 是 `assistant` → 挪到 `user` 侧（连续段合并成一条）
    - 原 role 已是 `user`（用户消息 / 调度转场）→ 原样保留，不参与合并
    - 无说话人标记（工具摘要）→ 原样保留，不参与合并
    - `【会话摘要】`/`【相关记忆】` → 原样透传（它们不是对话历史）

    只重排 role 与合并，**不改动任何正文**（`<name>:` 标记保留，与 GLOBAL_SETTING.md 的
    `<ID>` 约定同源）。

    单角色私聊天然退化为恒等变换：那里的历史要么是自己的（assistant，保持），要么是用户
    的（role=user，保持），所以逐条与原样返回等价 —— 私聊路径零回归。
    """
    ordered: list[dict] = []
    # 连续的「非本人 agent 发言」缓冲：一次 flush 成一条 user 消息，避免大量
    # user/assistant 交替的 ping-pong，同时保住先后顺序（块内仍带 `<name>:` 标记）
    foreign: list[str] = []

    def flush() -> None:
        if foreign:
            ordered.append({"role": "user", "content": "\n\n".join(foreign)})
            foreign.clear()

    for message in messages or []:
        content = str(message.get("content") or "")
        if content.lstrip().startswith(_NON_DIALOGUE_PREFIX):
            flush()
            ordered.append(dict(message))
            continue

        speaker = history_speaker(content)
        if speaker == self_id:
            flush()
            ordered.append({"role": "assistant", "content": content})
        elif speaker:
            if message.get("role") == "user":
                # 本来就是 user 侧（用户消息 / `<agent_e>:` 转场）：保持原样
                flush()
                ordered.append(dict(message))
            else:
                foreign.append(content)
        else:
            # 无作者信息可判：保持原 role（工具摘要等）
            flush()
            ordered.append(dict(message))
    flush()
    return ordered


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


# ── 提示词热重载 ────────────────────────────────────────────


def prompt_revision() -> tuple:
    """提示词热重载用的廉价快照：`(相对路径, mtime_ns, 大小)` 排序后成元组。

    任一提示词文件被改 / 增删（**包括 `@ref=` 指向的目标文件**，因为这里是
    递归扫整个提示词目录），或 `system_settings.json` 被改，快照都会变。
    只 stat 不读内容 —— 整个提示词目录也就几十个文件，代价可忽略。

    排除 `.bak`（dashboard 备份）与 `.tmp`（原子写中间态）：它们的出现/变化
    不代表「生效内容」变了，没必要因此重建一次。
    """
    root = config.SYSTEM_PROMPT_DIR
    items: list[tuple[str, int, int]] = []
    try:
        for path in root.rglob("*"):
            try:
                if not path.is_file():
                    continue
                if path.name.endswith(".bak") or path.name.endswith(".tmp"):
                    continue
                stat = path.stat()
            except OSError:
                continue
            items.append((str(path.relative_to(root)), stat.st_mtime_ns, stat.st_size))
    except OSError:
        pass

    try:
        stat = config.SYSTEM_SETTINGS_PATH.stat()
        items.append((str(config.SYSTEM_SETTINGS_PATH), stat.st_mtime_ns, stat.st_size))
    except OSError:
        items.append((str(config.SYSTEM_SETTINGS_PATH), 0, 0))

    items.sort()
    return tuple(items)


def _read_prompt_segment(
    path: Path, *, agent_id: str, filename: str, allow_when: bool = True
) -> tuple[str, str]:
    """读一个提示词文件并解析开头指令区，返回 `(正文, @when 取值)`。

    - `@ref=` 在读取期整文件替换；`@when=` 摘掉并回传（`""` = 无条件）
    - `allow_when=False` 时遇到 `@when=` 报错（该文件类型没有条件注入语义）
    - 解析失败抛 `prompt_refs.PromptRefError`（不静默降级）
    """
    raw = path.read_text(encoding="utf-8")
    resolved = prompt_refs.resolve_segment(
        raw,
        agent_id=agent_id,
        filename=filename,
        system_prompt_dir=config.SYSTEM_PROMPT_DIR,
        allow_when=allow_when,
    )
    return resolved.text, resolved.when


def _read_prompt_text(path: Path, *, agent_id: str, filename: str) -> str:
    """只取正文的读取入口（`allow_when=False`）。

    TOOL.md / world_info.md 这类「正文另作他用」或「内容不参与普通段注入」的文件走这条：
    它们没有条件注入的语义，写了 `@when=` 会直接报错而不是被静默忽略。
    """
    return _read_prompt_segment(
        path, agent_id=agent_id, filename=filename, allow_when=False
    )[0]


def _assert_process_declarative(prompt_dir: Path, filename: str, *, agent_id: str) -> None:
    """PROCESS 声明文件的内容根本不被读取：写了指令无意义，显式报错。"""
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
    if prompt_refs.has_when_directive(text):
        raise prompt_refs.PromptRefError(
            "PROCESS 声明文件（process*.md）的内容不会被读取，@when= 不生效；"
            "如需条件注入请改其他文件",
            agent_id=agent_id,
            filename=filename,
            line=prompt_refs.first_when_line(text) or "",
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

    - files 里写 `TOOL.md`：读该文件（支持 `@ref=` 跳到另一处，见 prompt_refs），
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
    # 「第几段 → 注入条件」（`@when=` 取值，见 prompt_refs.WHEN_*）。
    # 构建期只记录，发送期按当次请求是否群聊过滤（build_context 的 effective_system_prompt）。
    when_rules: list[tuple[int, str]] = field(default_factory=list)


def _add_segment(parts: SystemPromptParts, content: str, when: str = "") -> int:
    """追加一个 system 段并登记注入条件，返回段下标。"""
    parts.messages.append({"role": "system", "content": content})
    index = len(parts.messages) - 1
    if when:
        parts.when_rules.append((index, when))
    return index


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
        - `@ref=` 跳转在**读取期**逐文件解析（可指向另一目录的同名文件，或
          `目录/文件名` 形式的任意文件；最多 `prompt_refs.MAX_REF_DEPTH` 跳）
        - `{agent_id}` 占位符在**拼接完成后**统一替换（简单规则：所有提示词段 + 记录串）
        """
        agent = self._agent
        prompt_dir = config.SYSTEM_PROMPT_DIR / agent.id
        global_setting = config.SYSTEM_PROMPT_DIR / "GLOBAL_SETTING.md"

        parts = SystemPromptParts()

        if global_setting.exists():
            raw_content, when = _read_prompt_segment(
                global_setting, agent_id=agent.id, filename="GLOBAL_SETTING.md"
            )
            content = raw_content.strip()
            if content:
                _add_segment(parts, content, when)

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
            raw_content, when = _read_prompt_segment(
                file_path, agent_id=agent.id, filename=filename
            )
            content = raw_content.strip()
            if content:
                index = _add_segment(parts, content, when)
                # 记录 main 的 IDENTITY.md 位置，供杂项开关在调用模型前拦截
                if agent.id == "main" and str(filename).lower() == "identity.md":
                    parts.identity_indexes.append(index)
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

    def effective_system_prompt(self, *, is_group: bool = False) -> list[dict[str, str]]:
        """按「当次请求」过滤系统提示词段，两类条件合并成一次过滤（保持下标稳定）。

        - `main` 关闭杂项「读 IDENTITY.md」→ 跳过记录下来的 IDENTITY 段
        - `@when=group` / `@when=single` → 与当次请求是否群聊不符的段跳过

        system prompt 是**实例级**构建并缓存复用的，而这两个条件都是**请求级**的，
        所以只能在这里（发送期）过滤，不能在构建期丢弃。
        """
        agent = self._agent
        blocked: set[int] = set()

        if (
            agent.id == "main"
            and getattr(agent, "_identity_system_indexes", None)
            and not identity_read_enabled()
        ):
            blocked.update(agent._identity_system_indexes)

        for index, when in getattr(agent, "_when_system_rules", None) or []:
            if (when == prompt_refs.WHEN_GROUP) != bool(is_group):
                blocked.add(index)

        if not blocked:
            return agent.system_prompt
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

        # 短期 session 记忆（对话历史）：先按说话人重分配 role，再接在免责框下面。
        # 免责框（HISTORY_DISCLAIMER）作用范围是**它下面的整个历史块**
        # （【会话摘要】/【相关记忆】/ 对话历史），见 shape_history_messages 的说明。
        # 摘要与相关记忆按当前决定原样透传，只重排对话历史这一段。
        history_messages = agent.context_client.search_context(
            user_id=task.user.id,
            session_id=task.user.session_id,
            agent_id=agent.id,
            query=content,
            max_messages=int(self._config.get("max_messages", 6) or 6),
            max_tokens=int(self._config.get("context_max_tokens", 3000) or 3000),
            commit_limit=int(self._config.get("commit_limit", 0) or 0),
        )
        long_context_message = [
            {"role": "system", "content": HISTORY_DISCLAIMER}
        ] + shape_history_messages(history_messages, self_id=agent.id)

        # 群聊判定（用于 @when=group / @when=single）：按当次请求的 session_id / user_id
        # 前缀。实例级缓存的 system prompt 必须在这里按请求过滤，见 effective_system_prompt。
        is_group = is_group_session(
            getattr(getattr(task, "user", None), "session_id", ""),
            getattr(getattr(task, "user", None), "id", ""),
        )
        system_prompt_messages = self.effective_system_prompt(is_group=is_group)

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

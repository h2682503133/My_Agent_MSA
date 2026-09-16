"""提示词指令解析（`@ref=` 跳转 / `@when=` 注入条件）与占位符替换（`{agent_id}`）。

构建 system prompt 时，一个 `.md` 文件的开头可以是**指令区**，正文也可以来自别处：

1. `@ref=<agent_id>` —— **整文件替换**为「另一个智能体的同名文件」
   - 格式：`@ref=alice`（指令行独占一行）
   - 整文件替换语义：写了 `@ref=` 就不能再有正文，否则报错
   - 目标 agent 目录 / 同名文件不存在 → 报错；目标内容为空 → 报错
   - **只允许一层**：目标文件内部再出现 `@ref=` / `@when=` → 报错
   - 失败一律抛 `PromptRefError`：宁可让任务直接失败，也不要静默用错人设

2. `@when=group` / `@when=single` —— 该段**只在群聊 / 只在非群聊**注入
   - 条件属于**写它的那个 agent**（声明方 / 引用方），与正文来自哪里无关
   - 目标文件里出现 `@when=` → 报错（避免"共享内容自带开关"的隐式耦合）
   - 判定时机是**发送期**（按 task 的 session_id / user_id），见
     `context_builder.is_group_session`

3. `{agent_id}` —— 替换为**当前请求方**的 agent id（不是目标方的）
   - 只替换白名单键，其余花括号原样保留（如正文里的 `{好感度：65}`）
   - 由 `context_builder.build_system_prompt()` 在拼接完成后统一处理

指令区规则（`@ref=` 与 `@when=` 共用一套）：
- 位于**文件开头**：连续的「指令行或空行」；出现第一行正文即结束
- 每条指令**最多一行**，格式严格（行尾不能有其它内容，取值白名单）
- **指令行不进入注入内容**：读取期就被摘掉，下游看不到
- 正文里再出现 `@ref=` / `@when=` → 报错，避免误写被静默吞掉

解析只发生在**读取期**，磁盘文件不会被改写；下游（send / 阶段1 / 阶段2 / identity 拦截）
只看到最终正文，不需要知道"跳转"与"条件"的存在。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# 跳转指令：`@ref=alice` / `@ref = alice`
REF_MARKER = "@ref="
_REF_DETECT_RE = re.compile(r"@ref\s*=")
_REF_LINE_RE = re.compile(r"^[ \t]*@ref\s*=\s*(\S+)[ \t]*$")

# 注入条件指令：`@when=group` / `@when=single`
WHEN_MARKER = "@when="
WHEN_GROUP = "group"
WHEN_SINGLE = "single"
WHEN_VALUES = (WHEN_GROUP, WHEN_SINGLE)
_WHEN_DETECT_RE = re.compile(r"@when\s*=")
_WHEN_LINE_RE = re.compile(r"^[ \t]*@when\s*=\s*(\S+)[ \t]*$")

# agent_id 白名单字符（现有 agent_list 里的 id 都是这种；顺带排除 `@ref={agent_id}` 这类自指写法）
_AGENT_ID_RE = re.compile(r"^[A-Za-z0-9_.@-]+$")

# 占位符：只认这些键，其它花括号一律原样保留
PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
SUBSTITUTABLE_KEYS = ("agent_id",)


class PromptRefError(ValueError):
    """指令解析失败。构建 system prompt 时直接抛出，不做静默降级。"""

    def __init__(self, reason: str, *, agent_id: str = "", filename: str = "", line: str = ""):
        parts = [f"[prompt-ref] {reason}"]
        if agent_id:
            parts.append(f"agent={agent_id}")
        if filename:
            parts.append(f"file={filename}")
        if line:
            parts.append(f"line={line.strip()}")
        super().__init__(" | ".join(parts))


@dataclass(frozen=True)
class ResolvedSegment:
    """一个提示词段的读取结果：正文 + 注入条件（`""` = 无条件）。"""

    text: str
    when: str = ""


# ── 指令探测（只判断"有没有"，用于报错与兼容检查）────────────────


def _first_ref(text: str) -> tuple[int, str] | None:
    """返回首个含 `@ref=` 的行号（0 基）与整行文本；没有则 None。"""
    for index, line in enumerate((text or "").splitlines()):
        if _REF_DETECT_RE.search(line):
            return index, line
    return None


def first_ref_line(text: str) -> str | None:
    """返回首个含 `@ref=` 的行（原始行，便于报错定位）；没有则 None。"""
    found = _first_ref(text)
    return found[1] if found else None


def has_ref_directive(text: str) -> bool:
    return _first_ref(text) is not None


def _first_when(text: str) -> tuple[int, str] | None:
    for index, line in enumerate((text or "").splitlines()):
        if _WHEN_DETECT_RE.search(line):
            return index, line
    return None


def first_when_line(text: str) -> str | None:
    """返回首个含 `@when=` 的行（原始行，便于报错定位）；没有则 None。"""
    found = _first_when(text)
    return found[1] if found else None


def has_when_directive(text: str) -> bool:
    return _first_when(text) is not None


def has_any_directive(text: str) -> bool:
    return has_ref_directive(text) or has_when_directive(text)


# ── 文件查找 / 占位符 ────────────────────────────────────────


def resolve_named_file(directory: Path, filename: str) -> Path | None:
    """在目录里按文件名找文件：先精确，再大小写不敏感（对齐 world_info.md 的既有行为）。"""
    direct = directory / filename
    if direct.is_file():
        return direct
    try:
        for candidate in directory.iterdir():
            if candidate.is_file() and candidate.name.lower() == str(filename).lower():
                return candidate
    except OSError:
        return None
    return None


def substitute_placeholders(text: str, *, agent_id: str) -> str:
    """白名单占位符替换；没有命中的键时原样返回（同一个对象，零拷贝）。"""
    if not text or "{" not in text:
        return text
    values = {"agent_id": agent_id}

    def _replace(match: re.Match) -> str:
        key = match.group(1)
        if key not in values:
            return match.group(0)  # 未知键（含中文花括号等非 ASCII 键）保持原样
        return str(values[key])

    return PLACEHOLDER_RE.sub(_replace, text)


# ── 读取期解析 ──────────────────────────────────────────────


def resolve_segment(
    raw_text: str,
    *,
    agent_id: str,
    filename: str,
    system_prompt_dir: Path,
    allow_when: bool = True,
) -> ResolvedSegment:
    """读取期解析一个提示词段：摘掉指令区，按需做 `@ref=` 整文件替换。

    - 无任何指令 → 原样返回（现有文件全走这条路，零拷贝、字节不变）
    - `allow_when=False`（TOOL.md / world_info.md 等不支持条件注入的文件类型）
      遇到 `@when=` → 报错，而不是静默忽略
    - 任何校验失败 → 抛 PromptRefError
    """
    if not has_any_directive(raw_text):
        return ResolvedSegment(text=raw_text, when="")

    ref_target, when, body = _split_directives(raw_text, agent_id=agent_id, filename=filename)

    if when and not allow_when:
        raise PromptRefError(
            "该文件类型不支持 @when=（只支持 files 里声明的普通 .md 提示词段）",
            agent_id=agent_id,
            filename=filename,
            line=first_when_line(raw_text) or "",
        )

    if ref_target is None:
        return ResolvedSegment(text=body, when=when)

    # @ref= 是「整文件替换」：正文会被丢弃，因此必须没有正文
    leftovers = [ln for ln in body.splitlines() if ln.strip()]
    if leftovers:
        raise PromptRefError(
            f"指令行之外还有 {len(leftovers)} 行内容（整文件替换语义下会被丢弃），"
            "请让 @ref= 独占整份文件",
            agent_id=agent_id,
            filename=filename,
            line=first_ref_line(raw_text) or "",
        )
    if not _AGENT_ID_RE.match(ref_target):
        raise PromptRefError(
            f"目标 agent id 非法：{ref_target!r}（只允许字母数字与 _ . @ - ）",
            agent_id=agent_id,
            filename=filename,
            line=first_ref_line(raw_text) or "",
        )
    if ref_target == agent_id:
        raise PromptRefError(
            "@ref= 指向自己，无意义",
            agent_id=agent_id,
            filename=filename,
            line=first_ref_line(raw_text) or "",
        )

    text = _read_target(
        ref_target, filename=filename, agent_id=agent_id, system_prompt_dir=system_prompt_dir
    )
    return ResolvedSegment(text=text, when=when)


def resolve_text(
    raw_text: str,
    *,
    agent_id: str,
    filename: str,
    system_prompt_dir: Path,
    allow_when: bool = False,
) -> str:
    """只取正文的兼容入口（`resolve_segment` 的薄包装）。

    默认 `allow_when=False`：调用方拿不到条件，遇到 `@when=` 就报错，避免静默失灵。
    """
    return resolve_segment(
        raw_text,
        agent_id=agent_id,
        filename=filename,
        system_prompt_dir=system_prompt_dir,
        allow_when=allow_when,
    ).text


def _split_directives(
    raw_text: str, *, agent_id: str, filename: str
) -> tuple[str | None, str, str]:
    """按「文件开头的指令区 + 正文」拆分，返回 `(ref 目标, when 取值, 正文)`。

    指令区 = 文件开头连续的「空行或指令行」；第一行正文出现即结束。
    指令区里 `@ref=` / `@when=` 各最多一行，重复或格式非法都报错。
    """
    lines = (raw_text or "").splitlines()
    ref_target: str | None = None
    when = ""
    body_start = 0

    for index, line in enumerate(lines):
        if not line.strip():
            body_start = index + 1
            continue
        if _REF_DETECT_RE.search(line):
            match = _REF_LINE_RE.match(line)
            if match is None:
                raise PromptRefError(
                    "指令行格式非法：@ref= 必须独占一行且形如 `@ref=alice`（行尾不能有其它内容）",
                    agent_id=agent_id,
                    filename=filename,
                    line=line,
                )
            if ref_target is not None:
                raise PromptRefError(
                    "@ref= 只能写一行", agent_id=agent_id, filename=filename, line=line
                )
            ref_target = match.group(1).strip()
            body_start = index + 1
            continue
        if _WHEN_DETECT_RE.search(line):
            match = _WHEN_LINE_RE.match(line)
            if match is None:
                raise PromptRefError(
                    "指令行格式非法：@when= 必须独占一行且形如 `@when=group`（行尾不能有其它内容）",
                    agent_id=agent_id,
                    filename=filename,
                    line=line,
                )
            if when:
                raise PromptRefError(
                    "@when= 只能写一行", agent_id=agent_id, filename=filename, line=line
                )
            value = match.group(1).strip().lower()
            if value not in WHEN_VALUES:
                raise PromptRefError(
                    f"@when= 取值非法：{value!r}（只允许 {' / '.join(WHEN_VALUES)}）",
                    agent_id=agent_id,
                    filename=filename,
                    line=line,
                )
            when = value
            body_start = index + 1
            continue
        break

    body_lines = lines[body_start:]
    for line in body_lines:
        if _REF_DETECT_RE.search(line) or _WHEN_DETECT_RE.search(line):
            raise PromptRefError(
                "指令行必须位于文件开头（@ref= / @when= 之前不能有正文）",
                agent_id=agent_id,
                filename=filename,
                line=line,
            )
    return ref_target, when, "\n".join(body_lines)


def _read_target(target: str, *, filename: str, agent_id: str, system_prompt_dir: Path) -> str:
    """读取目标 agent 的同名文件，做存在性 / 非空 / 只允许一层的校验。"""
    target_dir = Path(system_prompt_dir) / target
    if not target_dir.is_dir():
        raise PromptRefError(
            f"目标智能体目录不存在：{target_dir}", agent_id=agent_id, filename=filename
        )

    target_file = resolve_named_file(target_dir, filename)
    if target_file is None:
        raise PromptRefError(
            f"目标文件不存在：{target_dir / filename}", agent_id=agent_id, filename=filename
        )

    try:
        text = target_file.read_text(encoding="utf-8")
    except Exception as exc:  # 读取失败也按硬失败处理
        raise PromptRefError(
            f"目标文件读取失败：{target_file}（{exc}）", agent_id=agent_id, filename=filename
        ) from exc

    if not text.strip():
        raise PromptRefError(
            f"目标文件内容为空：{target_file}", agent_id=agent_id, filename=filename
        )

    nested = first_ref_line(text)
    if nested is not None:
        raise PromptRefError(
            f"目标文件内部也含 @ref=（只允许一层跳转）：{target_file}",
            agent_id=agent_id,
            filename=filename,
            line=nested,
        )

    nested_when = first_when_line(text)
    if nested_when is not None:
        raise PromptRefError(
            f"目标文件内部含 @when=（注入条件要写在引用方文件里，不能随内容共享）：{target_file}",
            agent_id=agent_id,
            filename=filename,
            line=nested_when,
        )
    return text

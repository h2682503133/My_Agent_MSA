"""提示词引用解析（`@ref=`）与占位符替换（`{agent_id}`）。

构建 system prompt 时，一个 `.md` 文件的内容可能**不是它自己写的字**，而是两种间接形式：

1. `@ref=<agent_id>` —— **整文件替换**为「另一个智能体的同名文件」
   - 指令行必须独占整份文件（其余只有空白），否则报错；格式：`@ref=alice`
   - 目标 agent 目录 / 同名文件不存在 → 报错；目标内容为空 → 报错
   - **只允许一层**：目标文件内部再出现 `@ref=` → 报错（指向自己也会走这条判定，因为目标就是本文件）
   - 失败一律抛 `PromptRefError`：宁可让任务直接失败，也不要静默用错人设

2. `{agent_id}` —— 替换为**当前请求方**的 agent id（不是目标方的）
   - 只替换白名单键，其余花括号原样保留（如正文里的 `{好感度：65}`）
   - 由 `context_builder.build_system_prompt()` 在拼接完成后统一处理（简单规则：所有提示词段 + 记录串）

解析只发生在**读取期**，磁盘文件不会被改写；下游（send / 阶段1 / 阶段2 / identity 拦截）
只看到最终文本，不需要知道"跳转"存在。
"""

from __future__ import annotations

import re
from pathlib import Path

# 跳转指令：`@ref=alice` / `@ref = alice`
REF_MARKER = "@ref="
_REF_DETECT_RE = re.compile(r"@ref\s*=")
_REF_LINE_RE = re.compile(r"^[ \t]*@ref\s*=\s*(\S+)[ \t]*$")
# agent_id 白名单字符（现有 agent_list 里的 id 都是这种；顺带排除 `@ref={agent_id}` 这类自指写法）
_AGENT_ID_RE = re.compile(r"^[A-Za-z0-9_.@-]+$")

# 占位符：只认这些键，其它花括号一律原样保留
PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
SUBSTITUTABLE_KEYS = ("agent_id",)


class PromptRefError(ValueError):
    """`@ref=` 解析失败。构建 system prompt 时直接抛出，不做静默降级。"""

    def __init__(self, reason: str, *, agent_id: str = "", filename: str = "", line: str = ""):
        parts = [f"[prompt-ref] {reason}"]
        if agent_id:
            parts.append(f"agent={agent_id}")
        if filename:
            parts.append(f"file={filename}")
        if line:
            parts.append(f"line={line.strip()}")
        super().__init__(" | ".join(parts))


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


def resolve_text(raw_text: str, *, agent_id: str, filename: str, system_prompt_dir: Path) -> str:
    """读取期解析：命中 `@ref=` 就整文件替换成「目标 agent 的同名文件」内容。

    - 未命中 → 原样返回（现有文件全部走这条路，行为与改造前一致）
    - 命中但任何校验失败 → 抛 PromptRefError
    """
    line = first_ref_line(raw_text)
    if line is None:
        return raw_text
    target = _parse_directive(raw_text, line=line, agent_id=agent_id, filename=filename)
    return _read_target(target, filename=filename, agent_id=agent_id, system_prompt_dir=system_prompt_dir)


def _parse_directive(raw_text: str, *, line: str, agent_id: str, filename: str) -> str:
    """校验指令行并返回目标 agent id。"""
    match = _REF_LINE_RE.match(line)
    if match is None:
        raise PromptRefError(
            "指令行格式非法：@ref= 必须独占一行且形如 `@ref=alice`（行尾不能有其它内容）",
            agent_id=agent_id,
            filename=filename,
            line=line,
        )

    # 指令行必须独占整份文件；按行号排除，不能按对象同一性判断
    found = _first_ref(raw_text)
    ref_index = found[0] if found else -1
    others = [
        ln for i, ln in enumerate(raw_text.splitlines()) if i != ref_index and ln.strip()
    ]
    if others:
        raise PromptRefError(
            f"指令行之外还有 {len(others)} 行内容（整文件替换语义下会被丢弃），请让 @ref= 独占整份文件",
            agent_id=agent_id,
            filename=filename,
            line=line,
        )

    target = match.group(1).strip()
    if not _AGENT_ID_RE.match(target):
        raise PromptRefError(
            f"目标 agent id 非法：{target!r}（只允许字母数字与 _ . @ - ）",
            agent_id=agent_id,
            filename=filename,
            line=line,
        )
    if target == agent_id:
        raise PromptRefError(
            "@ref= 指向自己，无意义", agent_id=agent_id, filename=filename, line=line
        )
    return target


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
    return text

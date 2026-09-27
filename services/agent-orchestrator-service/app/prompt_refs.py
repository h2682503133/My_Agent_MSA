"""提示词指令解析（`@ref=` 跳转 / `@when=` 注入条件）与占位符替换（`{agent_id}`）。

构建 system prompt 时，一个 `.md` 文件的开头可以是**指令区**，正文也可以来自别处：

1. `@ref=<目标>` —— **整文件替换**为目标文件的内容
   - 格式：`@ref=alice`（指令行独占一行）
   - 三种目标写法：
     - **含 `/`**：提示词目录内的**相对路径**，文件名可以随便叫。
       `main/SOUL.md` 写 `@ref=人设合集/凌伊.md` → 读该文件
     - **不含 `/`、末段带 `.md` / `.json`**：**根目录下的文件**。
       `@ref=人设甲.md` → 读 `<SYSTEM_PROMPT_DIR>/人设甲.md`
     - **其余**（老写法）：另一个目录下的**同名文件**。`lilith/SOUL.md` 写
       `@ref=alice` → 读 `alice/SOUL.md`
   - 整文件替换语义：写了 `@ref=` 就不能再有正文，否则报错
   - 目标不存在 / 内容为空 / 指向自己 → 报错
   - **最多 `MAX_REF_DEPTH` 跳**（`角色存根 → hub → 人设文件` 是 2 跳）。超限报错并
     打印完整链；**刻意不做环检测**：成环必然在某一跳撞上上限，链打出来就能看出是环
   - 失败一律抛 `PromptRefError`：宁可让任务直接失败，也不要静默用错人设

2. `@when=group` / `@when=single` —— 该段**只在群聊 / 只在非群聊**注入
   - 条件属于**写它的那个 agent**（声明方 / 引用方），与正文来自哪里无关
   - 目标文件里出现 `@when=` → 报错（避免"共享内容自带开关"的隐式耦合）。
     这条**任何跳数下都成立**：条件只能写在最外层「使用者」的文件里
   - 判定时机是**发送期**（按 task 的 session_id / user_id），见
     `context_builder.is_group_session`

3. `{agent_id}` —— 替换为**当前请求方**的 agent id（不是目标方的）
   - 只替换白名单键，其余花括号原样保留（如正文里的 `{好感度：65}`）
   - 由 `context_builder.build_system_prompt()` 在拼接完成后统一处理
   - 链上永远取**最外层请求方**：`lilith → alice → 人设文件` 里，
     人设文件写 `你是 {agent_id}` 得到的是 `lilith`

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

# 跳转指令：`@ref=alice` / `@ref = alice` / `@ref=人设合集/凌伊.md`
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

# 跳转深度上限（**跳数**，不是层数）：角色存根 → hub → 人设文件 = 2 跳，留一跳余地。
# 刻意不做环检测：成环必然在某一跳撞上这个上限，而报错会打印完整链
# （看到 `a/SOUL.md → b/SOUL.md → a/SOUL.md` 就知道是环）。
MAX_REF_DEPTH = 3

# 目标里不允许的路径段：空段（`a//b`、结尾带 `/`）、`.`、`..`（挡目录穿越）
_INVALID_PATH_SEGMENTS = frozenset({"", ".", ".."})
# 盘符路径（`C:/x`）：绝对路径的另一种写法
_DRIVE_RE = re.compile(r"^[A-Za-z]:")
# 带这些后缀且不含 `/` 的目标，按「根目录下的文件」理解（见 is_path_form）
_FILE_SUFFIXES = (".md", ".json")

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


# ── 跳转目标：写法校验与定位 ────────────────────────────────


def is_path_form(target: str) -> bool:
    """是否按「提示词目录内的相对路径」理解这个目标。

    - 含 `/`：路径 —— `人设合集/凌伊.md`、`alice/SOUL.md`
    - 不含 `/` 但末段带 `.md` / `.json`：**根目录下的文件** —— `人设甲.md`
      （否则 `@ref=人设甲.md` 会被当成"一个叫 人设甲.md 的目录"，报出
      「目标目录不存在」这种把人带偏的错）
    - 其余：老语义 —— 另一个目录下的**同名文件**（`@ref=alice` → `alice/SOUL.md`）
    """
    if "/" in target:
        return True
    return target.lower().endswith(_FILE_SUFFIXES)


def ref_chain_root(agent_id: str, filename: str) -> list[str]:
    """跳转链的起点（最外层请求方）。只用于报错，不参与解析。"""
    return [f"{agent_id}/{filename}"]


def validate_ref_target(
    target: str, *, agent_id: str, filename: str, line: str = ""
) -> None:
    """校验 `@ref=` 目标写法（不含磁盘存在性检查）。

    只接受**提示词目录内的相对路径**：绝对路径 / 盘符 / `\\` / `..` /
    空段一律拒绝 —— 否则 `@ref=` 就能读到配置卷里任何文件。

    中文允许：老实现用 `[A-Za-z0-9_.@-]` 白名单，导致 `人设合集/凌伊.md`
    这种目标直接报"目标 agent id 非法"。
    """
    def _fail(reason: str) -> None:
        raise PromptRefError(reason, agent_id=agent_id, filename=filename, line=line)

    if "{" in target or "}" in target:
        _fail(f"@ref= 不做占位符替换，不能用 {target!r} 当跳转目标")
    if target.startswith("/") or target.startswith("\\") or "\\" in target:
        _fail(f"@ref= 只接受提示词目录内的相对路径：{target!r}（不能是绝对路径，也不能含 \\）")
    if _DRIVE_RE.match(target):
        _fail(f"@ref= 只接受提示词目录内的相对路径：{target!r}（不能是盘符路径）")
    for segment in target.split("/"):
        if segment in _INVALID_PATH_SEGMENTS:
            _fail(
                f"@ref= 目标含非法路径段：{target!r}"
                "（不允许空段 / . / .. —— 空段常见于结尾多写了 `/`，"
                "带 `/` 时请把文件名写全，如 `人设合集/凌伊.md`）"
            )


def display_path(path: Path, root: Path) -> str:
    """报错里用的短路径（提示词目录内显示相对路径，之外退回绝对路径）。

    统一用 `/` 分隔：报错里显示的是**写进文件的写法**，Windows 上的 `\\`
    会与 `@ref=` 的语法混淆（而且 `@ref=` 本身不接受 `\\`）。
    """
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def locate_ref_target(
    target: str, *, filename: str, system_prompt_dir: Path
) -> tuple[Path | None, str]:
    """把跳转目标定位到磁盘文件，返回 `(路径, 失败说明)`；定位失败时路径为 None。"""
    root = Path(system_prompt_dir)
    if not is_path_form(target):
        directory = root / target
        if not directory.is_dir():
            return None, f"目标目录不存在：{directory}（@ref={target}）"
        found = resolve_named_file(directory, filename)
        if found is None:
            return None, f"目标文件不存在：{directory / filename}（@ref={target}）"
        return found, ""

    parent, _, name = target.rpartition("/")
    directory = root / parent
    if not directory.is_dir():
        return None, f"目标目录不存在：{directory}（@ref={target}）"
    found = resolve_named_file(directory, name)
    if found is None:
        return None, f"目标文件不存在：{directory / name}（@ref={target}）"
    return found, ""


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
    line = first_ref_line(raw_text) or ""
    validate_ref_target(ref_target, agent_id=agent_id, filename=filename, line=line)
    if not is_path_form(ref_target) and ref_target == agent_id:
        raise PromptRefError(
            "@ref= 指向自己，无意义",
            agent_id=agent_id,
            filename=filename,
            line=line,
        )

    text = _read_target(
        ref_target,
        filename=filename,
        agent_id=agent_id,
        system_prompt_dir=Path(system_prompt_dir),
        chain=ref_chain_root(agent_id, filename),
        depth=1,
        line=line,
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


def _read_target(
    target: str,
    *,
    filename: str,
    agent_id: str,
    system_prompt_dir: Path,
    chain: list[str],
    depth: int,
    line: str = "",
) -> str:
    """解析一跳 `@ref=`：读目标文件；若目标自己也是整文件跳转，就继续往下（受深度上限约束）。

    `chain` 是已走过的链（仅用于报错），`depth` 是当前跳数（从 1 开始）。
    `filename` 沿链传播：链中某一跳用**老写法** `@ref=alice` 时取的是**最外层请求的
    那个文件名** —— `lilith/SOUL.md → alice/SOUL.md → @ref=bob → bob/SOUL.md`，
    所以现有指针文件一个字都不用改。

    不做环检测：成环必然在某一跳超过 `MAX_REF_DEPTH`，而报错会打印完整链。
    """
    root = Path(system_prompt_dir)
    target_file, problem = locate_ref_target(
        target, filename=filename, system_prompt_dir=root
    )
    if target_file is None:
        raise PromptRefError(
            f"{problem}｜链：{' → '.join(chain + [target])}",
            agent_id=agent_id,
            filename=filename,
            line=line,
        )

    try:
        text = target_file.read_text(encoding="utf-8")
    except Exception as exc:  # 读取失败也按硬失败处理
        raise PromptRefError(
            f"目标文件读取失败：{target_file}（{exc}）"
            f"｜链：{' → '.join(chain + [display_path(target_file, root)])}",
            agent_id=agent_id,
            filename=filename,
            line=line,
        ) from exc

    rel = display_path(target_file, root)
    chain_now = chain + [rel]

    if not text.strip():
        raise PromptRefError(
            f"目标文件内容为空：{rel}｜链：{' → '.join(chain_now)}",
            agent_id=agent_id,
            filename=filename,
            line=line,
        )

    # 普通内容（绝大多数情况）：原样返回，字节不变
    if not has_any_directive(text):
        return text

    nested_ref, nested_when, body = _split_directives(
        text, agent_id=agent_id, filename=filename
    )
    if nested_when:
        raise PromptRefError(
            f"目标文件内含 @when=（注入条件要写在引用方文件里，不能随内容共享）：{rel}"
            f"｜链：{' → '.join(chain_now)}",
            agent_id=agent_id,
            filename=filename,
            line=first_when_line(text) or "",
        )
    if nested_ref is None:
        # 抽掉指令区后就是普通正文（例如目标文件里写了 `@when=` 之外的规范指令区）
        return body

    # 目标自己也是整文件跳转 → 同样不能带正文
    leftovers = [ln for ln in body.splitlines() if ln.strip()]
    if leftovers:
        raise PromptRefError(
            f"目标文件 {rel} 的 @ref= 之外还有 {len(leftovers)} 行内容"
            "（整文件替换语义下会被丢弃）"
            f"｜链：{' → '.join(chain_now)}",
            agent_id=agent_id,
            filename=filename,
            line=first_ref_line(text) or "",
        )
    if depth >= MAX_REF_DEPTH:
        raise PromptRefError(
            f"@ref= 跳转超过 {MAX_REF_DEPTH} 跳（成环也会在这里被拦下）"
            f"｜链：{' → '.join(chain_now + [nested_ref])}",
            agent_id=agent_id,
            filename=filename,
            line=first_ref_line(text) or "",
        )
    validate_ref_target(
        nested_ref, agent_id=agent_id, filename=filename, line=first_ref_line(text) or ""
    )
    return _read_target(
        nested_ref,
        filename=filename,
        agent_id=agent_id,
        system_prompt_dir=root,
        chain=chain_now,
        depth=depth + 1,
        line=line,
    )

"""TOOL.json 工具目录的读取 / 选项解析 / 预览渲染 / TOOL.md 生成。

dashboard 与 orchestrator 是两个独立容器，没法直接复用代码，所以这里实现一份
与 `agent-orchestrator-service/app/context_builder.py` **规则一致**的选择与渲染：

- `sections`：`["*"]` 全部 / `"!段id"` 排除 / `{"include": [], "exclude": []}`
- `tools`：token 命中 工具名 / 段 id / 工具的 `groups` 标签
- 目录里 `"enabled": false` 的段或工具：默认不注入，被选项点名才注入
- 正文（TOOL.md 里选项块之外的内容）默认接在注入内容之后（`body_position: before` 可前置）

用途：给系统提示词页面提供「按 TOOL.json 勾选 → 生成智能体 TOOL.md」与注入预览。
**改注入规则时，这里和 context_builder.py 必须同步改。**
"""

from __future__ import annotations

import json
import re
from pathlib import Path

# 目录文件名（system_prompt 根目录，与 GLOBAL_SETTING.md 同级）
CATALOG_FILENAME = "TOOL.json"
# TOOL.md 里选项块的围栏标签
OPTION_BLOCK_TAG = "tool-inject"

_OPTION_BLOCK_RE = re.compile(
    r"```[ \t]*" + OPTION_BLOCK_TAG + r"[ \t]*\r?\n(.*?)```",
    re.DOTALL | re.IGNORECASE,
)


# ─── 读取 ────────────────────────────────────────────────────


def load_catalog(path: Path) -> tuple[dict, str]:
    """读取 TOOL.json，返回 (catalog, json_error)；文件不存在/损坏时 catalog 为 {}。"""
    if not path.exists():
        return {}, ""
    try:
        raw = path.read_text(encoding="utf-8")
    except Exception as exc:
        return {}, f"读取失败：{exc}"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return {}, f"JSON 解析失败：{exc}"
    if not isinstance(data, dict):
        return {}, "顶层必须是对象"
    return data, ""


def summarize_catalog(catalog: dict) -> dict:
    """把目录整理成前端好用的结构（段 + 工具，保留 enabled / groups 等标记）。"""
    sections = []
    for section in catalog.get("sections") or []:
        if not isinstance(section, dict):
            continue
        tools = []
        for tool in section.get("tools") or []:
            if not isinstance(tool, dict):
                continue
            tools.append({
                "name": str(tool.get("name") or "").strip(),
                "args": str(tool.get("args") or "").strip(),
                "desc": str(tool.get("desc") or "").strip(),
                "notes": [str(n) for n in (tool.get("notes") or []) if str(n).strip()],
                "groups": [str(g) for g in (tool.get("groups") or []) if str(g).strip()],
                "enabled": tool.get("enabled", True) is not False,
            })
        sections.append({
            "id": str(section.get("id") or "").strip(),
            "title": str(section.get("title") or "").strip(),
            "note": str(section.get("note") or "").strip(),
            "enabled": section.get("enabled", True) is not False,
            "kind": "tools" if tools else "text",
            "text": str(section.get("text") or "").strip() if not tools else "",
            "tools": tools,
        })
    return {
        "title": str(catalog.get("title") or "").strip(),
        "version": catalog.get("version"),
        "sections": sections,
        "section_count": len(sections),
        "tool_count": sum(len(s["tools"]) for s in sections),
    }


def collect_groups(summary: dict) -> list[str]:
    """目录里出现过的所有 groups 标签（前端做快捷筛选用）。"""
    groups: list[str] = []
    for section in summary.get("sections") or []:
        for tool in section.get("tools") or []:
            for group in tool.get("groups") or []:
                if group not in groups:
                    groups.append(group)
    return groups


# ─── 选项块解析 / 生成 ───────────────────────────────────────


def parse_tool_md(text: str) -> tuple[dict | None, str]:
    """拆出 TOOL.md 的选项块与正文；无块或 JSON 非法返回 (None, 原文)。"""
    raw = text or ""
    match = _OPTION_BLOCK_RE.search(raw)
    if not match:
        return None, raw.strip()
    try:
        options = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None, raw.strip()
    if not isinstance(options, dict):
        return None, raw.strip()
    return options, (raw[: match.start()] + raw[match.end():]).strip()


def build_tool_md(options: dict, body: str = "") -> str:
    """按选项生成 TOOL.md 全文（选项块 + 可选正文）。"""
    cleaned = {k: v for k, v in (options or {}).items() if v not in (None, [], {}, "")}
    block = "```" + OPTION_BLOCK_TAG + "\n" + json.dumps(cleaned, ensure_ascii=False, indent=2) + "\n```"
    body = (body or "").strip()
    return f"{block}\n\n{body}" if body else f"{block}\n"


# ─── 选择规则（与 context_builder.py 一致）───────────────────


def normalize_selector(value, default_all: bool = True) -> tuple[set[str], set[str]]:
    """把段 / 工具选择器统一成 (include, exclude) 两组小写 token。"""
    include: set[str] = set()
    exclude: set[str] = set()

    def _add(token, target):
        token = str(token or "").strip().lower()
        if token:
            target.add(token)

    if isinstance(value, dict):
        raw_include = value.get("include")
        if isinstance(raw_include, (list, tuple, set)):
            for item in raw_include:
                _add(item, include)
        elif raw_include is not None:
            _add(raw_include, include)
        raw_exclude = value.get("exclude")
        if isinstance(raw_exclude, (list, tuple, set)):
            for item in raw_exclude:
                _add(item, exclude)
        elif raw_exclude is not None:
            _add(raw_exclude, exclude)
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            token = str(item or "").strip()
            if token.startswith("!"):
                _add(token[1:], exclude)
            else:
                _add(token, include)
    elif value is not None:
        _add(value, include)

    if not include and default_all:
        include = {"*"}
    return include, exclude


def is_selected(name, groups, include: set[str], exclude: set[str]) -> bool:
    tokens = {str(name or "").strip().lower()}
    for group in groups or []:
        tokens.add(str(group or "").strip().lower())
    tokens.discard("")
    if "*" in exclude or (tokens & exclude):
        return False
    return "*" in include or bool(tokens & include)


def explicitly_named(include: set[str], token: str) -> bool:
    """include 里直接点名（`*` 与 groups 标签不算）。"""
    token = str(token or "").strip().lower()
    return bool(token) and token in include


def render_tool_line(tool: dict) -> str:
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


def render_section(section: dict, include_tools: set[str], exclude_tools: set[str]) -> str:
    title = str(section.get("title") or "").strip()
    text = str(section.get("text") or "").strip()
    if text:
        return f"{title}\n{text}" if title else text

    lines = []
    for tool in section.get("tools") or []:
        if not isinstance(tool, dict):
            continue
        tool_name = str(tool.get("name") or "").strip()
        if tool.get("enabled", True) is False and not explicitly_named(include_tools, tool_name):
            continue
        if not is_selected(tool_name, tool.get("groups"), include_tools, exclude_tools):
            continue
        line = render_tool_line(tool)
        if line:
            lines.append(line)
    if not lines:
        return ""
    body = "\n".join(lines)
    return f"{title}\n\n{body}" if title else body


def render_catalog(catalog: dict, options: dict | None = None) -> str:
    """按选项渲染目录（= orchestrator 实际注入的文本，用于预览）。"""
    options = options or {}
    include_sections, exclude_sections = normalize_selector(options.get("sections"))
    include_tools, exclude_tools = normalize_selector(options.get("tools"))

    blocks: list[str] = []
    if options.get("show_title", True):
        title = str(catalog.get("title") or "").strip()
        if title:
            blocks.append(title)

    for section in catalog.get("sections") or []:
        if not isinstance(section, dict):
            continue
        section_id = str(section.get("id") or "").strip()
        if section.get("enabled", True) is False and not explicitly_named(include_sections, section_id):
            continue
        if not is_selected(section_id, [], include_sections, exclude_sections):
            continue
        rendered = render_section(section, include_tools, exclude_tools)
        if rendered:
            blocks.append(rendered)
    return "\n\n".join(blocks)


def build_injection(catalog: dict, options: dict | None, body: str = "") -> str:
    """完整注入内容 = 目录渲染 + 正文（body_position=before 时前置）。

    与 orchestrator 一致：`"enabled": false` 时该智能体完全不注入工具段（正文也不注入）。
    """
    options = options or {}
    if options.get("enabled", True) is False:
        return ""
    text = render_catalog(catalog, options)
    body = (body or "").strip()
    if not body:
        return text
    if str(options.get("body_position") or "after").strip().lower() == "before":
        return f"{body}\n\n{text}" if text else body
    return f"{text}\n\n{body}" if text else body


# ─── 勾选 → 选项（前端面板用）────────────────────────────────


def default_options(summary: dict) -> dict:
    """默认勾选：目录里默认注入的段全选（默认关闭的段留给用户点名）。"""
    ids = [s["id"] for s in summary.get("sections") or [] if s.get("enabled", True) and s.get("id")]
    return {"sections": ids}


def options_from_selection(summary: dict, selected_sections, selected_tools, body: str = "") -> dict:
    """把前端勾选结果转成选项块。

    - `selected_sections`: 勾选的段 id 集合（未勾选 = 不注入该段）
    - `selected_tools`: 勾选的工具名集合（未勾选 = 进 exclude）
    - 目录里默认关闭但被勾选的段，靠「在 sections 里点名」启用
    - 目录里默认关闭但被勾选的工具，靠「在 tools.include 里点名」启用

    注意：选项语义里「空的 sections 列表」等于全选（与不写 sections 一样），
    所以「一个段都不勾」必须显式表达：没有正文时写 `enabled: false`；
    有正文时排除全部段并关掉目录标题，实际只注入正文。
    """
    section_ids = [s["id"] for s in summary.get("sections") or [] if s.get("id")]
    picked_sections = [sid for sid in section_ids if sid in selected_sections]

    include_tools: list[str] = []
    exclude_tools: list[str] = []
    for section in summary.get("sections") or []:
        if section.get("id") not in selected_sections:
            continue
        for tool in section.get("tools") or []:
            name = tool.get("name")
            if not name:
                continue
            picked = name in selected_tools
            if picked:
                if tool.get("enabled", True) is False:
                    # 目录默认关闭的工具：必须点名才注入
                    include_tools.append(name)
            else:
                exclude_tools.append(name)

    if not picked_sections:
        if not (body or "").strip():
            return {"enabled": False}
        # 有正文但不勾任何段：排除全部段并去掉目录标题 → 实际只注入正文
        options: dict = {"sections": {"include": [], "exclude": ["*"]}, "show_title": False}
    else:
        options = {"sections": picked_sections}
    tools: dict = {}
    if include_tools:
        tools["include"] = ["*"] + include_tools
    if exclude_tools:
        tools["exclude"] = exclude_tools
    if tools:
        options["tools"] = tools
    return options


def selection_from_options(summary: dict, options: dict | None) -> tuple[set[str], set[str]]:
    """把已有选项块反解成前端勾选状态（段 id 集合 / 工具名集合）。"""
    options = options or {}
    include_sections, exclude_sections = normalize_selector(options.get("sections"), default_all=False)
    include_tools, exclude_tools = normalize_selector(options.get("tools"))

    picked_sections: set[str] = set()
    picked_tools: set[str] = set()
    for section in summary.get("sections") or []:
        sid = section.get("id") or ""
        if not sid:
            continue
        # 目录默认关闭的段：只有在 sections 里被点名才真的注入，勾选状态照此还原
        if section.get("enabled", True) is False and not explicitly_named(include_sections, sid):
            continue
        if not is_selected(sid, [], include_sections, exclude_sections):
            continue
        picked_sections.add(sid)
        for tool in section.get("tools") or []:
            name = tool.get("name")
            if not name:
                continue
            # 目录默认关闭的工具：只有被点名才算勾选
            if tool.get("enabled", True) is False:
                if explicitly_named(include_tools, name) and not is_selected(name, tool.get("groups"), set(), exclude_tools):
                    picked_tools.add(name)
                continue
            if is_selected(name, tool.get("groups"), include_tools, exclude_tools):
                picked_tools.add(name)
    return picked_sections, picked_tools


def validate_options(summary: dict, options: dict | None) -> list[str]:
    """给出勾选/选项里的可疑点，前端显示为提醒。"""
    warnings: list[str] = []
    options = options or {}
    if options.get("enabled", True) is False:
        return ["enabled=false：该智能体不会注入任何工具段"]

    known_sections = {s["id"] for s in summary.get("sections") or [] if s.get("id")}
    known_tools = {t["name"] for s in summary.get("sections") or [] for t in (s.get("tools") or []) if t.get("name")}
    known_groups = set(collect_groups(summary))

    raw_sections = options.get("sections")
    if isinstance(raw_sections, (list, tuple)) and not raw_sections:
        warnings.append("sections 是空列表，语义上等于“全选”；一个段都不想注入请用 enabled=false")

    include_sections, exclude_sections = normalize_selector(raw_sections, default_all=False)
    include_tools, exclude_tools = normalize_selector(options.get("tools"))

    for token in sorted((include_sections | exclude_sections) - {"*"}):
        if token not in {s.lower() for s in known_sections}:
            warnings.append(f"段 “{token}” 在目录里不存在，将被忽略")
    unknown_tools = [
        token for token in sorted((include_tools | exclude_tools) - {"*"})
        if token not in {t.lower() for t in known_tools}
        and token not in {g.lower() for g in known_groups}
        and token not in {s.lower() for s in known_sections}
    ]
    for token in unknown_tools:
        warnings.append(f"工具 “{token}” 在目录里不存在，将被忽略")

    include_t, exclude_t = include_tools, exclude_tools
    for section in summary.get("sections") or []:
        tools = section.get("tools") or []
        if not tools:
            continue
        if not is_selected(section.get("id"), [], include_sections, exclude_sections):
            continue
        remaining = [
            t for t in tools
            if (t.get("enabled", True) is not False or explicitly_named(include_t, t["name"]))
            and is_selected(t["name"], t.get("groups"), include_t, exclude_t)
        ]
        if not remaining:
            warnings.append(f"段 “{section.get('id')}” 的工具被全部排除，该段不会注入")
    if not render_catalog({"sections": summary.get("sections") or [], "title": summary.get("title")}, options):
        warnings.append("当前勾选不会产生任何注入内容")
    return warnings

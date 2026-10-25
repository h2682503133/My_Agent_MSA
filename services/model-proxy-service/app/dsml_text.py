"""DSML 原生调用标记的归一化（model-proxy 侧适配）。

模型（deepseek-v4-flash）会把训练期的工具调用标记写进 **content 正文**，形如：

    <||DSML|| calls>
    <||DSML|| invoke name="shell">
    <||DSML|| parameter name="command" string="true">ls -la</||DSML|| parameter>
    </||DSML|| invoke>
    </||DSML|| calls>

本系统只认文本协议 `工具调用:名字|参数…`，orchestrator 的解析器不认这套标记。实测的
两种坏法都出现过：

1. **只有标记**（正文没有协议行）→ 工具调用根本没执行，后面连环重试；
2. **协议行 + 标记**（模型补写了一份）→ 标记被多行参数吞进 shell 命令（解析器按
   "行首指令 + 吞到下一个指令"取参），命令带上一串 `</DSML>` 标签直接跑坏；
   而且这段文本会被记进历史/工具记录，模型下一轮又照着学。

所以在这里统一收拾：**标记从正文里拿掉**；若正文没有现成的 `工具调用:` 行，
则把标记里能解析出的 invoke 补成协议行（让调用真的发生），否则只删不补（避免重复调用）。

兼容 **ASCII `|` 与全角 `｜`（U+FF5C）** 两种竖线、`calls` / `tool_calls`、参数
`string="true"` 属性有无。

> 踩过的坑：线上真实输出用的是**全角竖线** `<｜｜DSML｜｜ calls>`，第一版正则只写了
> ASCII `\|`，于是整个标记原样漏过（模型输出 150 token 的标记块，一条都没被清掉）。
"""

from __future__ import annotations

import re

# 竖线：ASCII `|` 或全角 `｜`（U+FF5C），允许 1~2 个（真实输出常见双竖线）
_BAR = r"[|｜]"
_OPEN = r"<" + _BAR + r"{1,2}\s*DSML\s*" + _BAR + r"{1,2}"
_CLOSE = r"</" + _BAR + r"{1,2}\s*DSML\s*" + _BAR + r"{1,2}"

_CALLS_RE = re.compile(
    _OPEN + r"\s*(?:tool_)?calls\s*>(.*?)" + _CLOSE + r"\s*(?:tool_)?calls\s*>",
    re.S | re.I,
)
_INVOKE_RE = re.compile(
    _OPEN + r"\s*invoke\s+name\s*=\s*[\"']([^\"']*)[\"']\s*>(.*?)" + _CLOSE + r"\s*invoke\s*>",
    re.S | re.I,
)
_PARAM_RE = re.compile(
    _OPEN + r"\s*parameter\s+name\s*=\s*[\"']([^\"']*)[\"']"
    r"(?:\s+string\s*=\s*[\"'][^\"']*[\"'])?\s*>(.*?)" + _CLOSE + r"\s*parameter\s*>",
    re.S | re.I,
)
# 任意残留标记（开/闭、带不带关键字、孤立的闭合标签都算）
_ANY_MARKER_RE = re.compile(_OPEN + r"[^>]*>|" + _CLOSE + r"[^>]*>", re.I)

_SHELL_TOOLS = {"shell", "run-shell", "command"}
_PROTOCOL_HEAD = "工具调用:"


def has_dsml(text: str) -> bool:
    """正文里是否出现 DSML 标记。"""
    return bool(_ANY_MARKER_RE.search(str(text or "")))


def _protocol_line(tool_name: str, params: list[tuple[str, str]]) -> str:
    if tool_name in _SHELL_TOOLS and len(params) == 1 and params[0][0] == "command":
        return f"工具调用:shell|{params[0][1]}"
    args = "|".join(value for _, value in params)
    return f"工具调用:{tool_name}|{args}" if args else f"工具调用:{tool_name}"


def adapt_text(text: str) -> str:
    """把 DSML 标记从正文里拿掉；必要时补成协议行。

    - 正文已有 `工具调用:` 行 → 只删标记（模型已用协议表达，标记是重复/噪声）；
    - 正文没有 → 把能解析出的 invoke 转成 `工具调用:…` 追加到末尾（让调用真的发生）；
    - 解析不出 invoke 的坏块 → 整块删除，不留会教坏模型的残渣。
    """
    raw = str(text or "")
    if not _ANY_MARKER_RE.search(raw):
        return raw

    converted: list[str] = []

    def _repl_calls(match: re.Match) -> str:
        block = match.group(1)
        for invoke in _INVOKE_RE.finditer(block):
            name = (invoke.group(1) or "").strip()
            if not name:
                continue
            params = [
                ((p.group(1) or "").strip(), (p.group(2) or "").strip())
                for p in _PARAM_RE.finditer(invoke.group(2))
            ]
            converted.append(_protocol_line(name, params))
        return ""

    out = _CALLS_RE.sub(_repl_calls, raw)
    out = _INVOKE_RE.sub("", out)        # 裸 invoke（缺 calls 外壳）
    out = _ANY_MARKER_RE.sub("", out)    # 残余标记（含孤立闭合标签）
    out = "\n".join(line.rstrip() for line in out.splitlines()).strip()
    out = re.sub(r"\n{3,}", "\n\n", out)

    if converted and _PROTOCOL_HEAD not in out:
        tail = "\n".join(converted)
        out = f"{out}\n{tail}".strip() if out else tail
    return out

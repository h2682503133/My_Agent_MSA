"""`<tool-call>…</tool-call>` 包装的提级解析自检（离线，纯函数）。"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
ORCH = ROOT / "services" / "agent-orchestrator-service" / "app"
sys.path.insert(0, str(ORCH.parent))

from app import syntax_parser as sp  # noqa: E402

FAILS: list[str] = []
CHECKS = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global CHECKS
    CHECKS += 1
    if cond:
        print(f"  ok    {name}")
    else:
        FAILS.append(name)
        print(f"  FAIL  {name} {extra}")


def main() -> int:
    print("单块：正文保留、指令追加到末尾")
    src = "这是说明文字。\n\n<tool-call>image-gen|a cat|1440x960</tool-call>"
    out = sp.promote_tool_call_blocks(src)
    check("标签被移除", "<tool-call>" not in out and "</tool-call>" not in out, out)
    check("正文保留", out.startswith("这是说明文字。"), out)
    check("末尾有工具调用指令", out.rstrip().endswith("工具调用:image-gen|a cat|1440x960"), out)
    check("指令在正文之后（便于 _find_command_block 命中末行）",
          out.splitlines()[-1].startswith("工具调用:"), out)

    print("多行块压成一行")
    src = "<tool-call>image-gen|line1\nline2|1024x1024</tool-call>"
    out = sp.promote_tool_call_blocks(src)
    check("多行被压平", out.strip() == "工具调用:image-gen|line1 line2|1024x1024", out)

    print("多块：逐行追加")
    src = "hi\n<tool-call>a|1</tool-call>\n<tool-call>b|2</tool-call>"
    out = sp.promote_tool_call_blocks(src)
    tail = [line for line in out.splitlines() if line.startswith("工具调用:")]
    check("两条指令", tail == ["工具调用:a|1", "工具调用:b|2"], str(tail))

    print("大小写/下划线变体")
    for variant in ("<TOOL-CALL>x|1</TOOL-CALL>", "<tool_call>x|1</tool_call>",
                    "< tool-call >x|1< / tool-call >"):
        out = sp.promote_tool_call_blocks(variant)
        check(f"{variant[:16]}… 生效", out.strip() == "工具调用:x|1", out)

    print("无关文本不改变")
    for text in ("没有标签的普通回复", "", "工具调用:image-gen|原生写法|1024x1024"):
        check(f"{text[:18]!r} 原样返回", sp.promote_tool_call_blocks(text) == text, sp.promote_tool_call_blocks(text))

    print("空块被忽略")
    out = sp.promote_tool_call_blocks("text<tool-call>   </tool-call>")
    check("空块不产生空指令", "工具调用:" not in out, out)

    print("接线：parse_syntax 入口调用它")
    src_file = (ORCH / "syntax_parser.py").read_text(encoding="utf-8")
    check("入口处调用", "raw_text = promote_tool_call_blocks(raw_text)" in src_file)
    check("定义了函数", "def promote_tool_call_blocks(" in src_file)

    print()
    if FAILS:
        print(f"FAILED {len(FAILS)}: {FAILS}")
        return 1
    print(f"ALL OK — {CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

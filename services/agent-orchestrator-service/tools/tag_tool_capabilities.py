"""给 TOOL.json 的工具打「能力标签」（cap:xxx，写在现有 groups 里）。

与 tag_tool_catalog.py 的分工：
- tag_tool_catalog.py：打档位（L0–L4）
- 本脚本：打能力（cap:tool_runtime / cap:browser / …），供 capabilities 门禁过滤

用法：
  python -X utf8 tools/tag_tool_capabilities.py <TOOL.json> [...]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# 工具 → 能力标签（只标"可被关闭的能力"；基础工具不标，靠 tool_runtime 总开关）
CAPABILITIES: dict[str, list[str]] = {
    "browser": ["cap:browser"],
    "dsh": ["cap:external_exec"],
    "codex": ["cap:external_exec"],
    "web-search": ["cap:search"],
    "windows-file-copy": ["cap:host_files"],
    "windows-file-move": ["cap:host_files"],
    "image-gen": ["cap:image_gen"],
    "viking-search": ["cap:openviking"],
    "add-skill-to-viking": ["cap:openviking"],
    "clawhub-search": ["cap:clawhub"],
    "clawhub-install": ["cap:clawhub"],
    "skill-delete": ["cap:clawhub"],
    "fetch": ["cap:tool_runtime"],
}


def tag(path: Path) -> tuple[int, list[str]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    tagged = 0
    unknown: list[str] = []
    for section in data.get("sections") or []:
        for tool in section.get("tools") or []:
            if not isinstance(tool, dict):
                continue
            name = str(tool.get("name") or "").strip()
            caps = CAPABILITIES.get(name)
            if not caps:
                unknown.append(name)
                continue
            groups = [str(item) for item in (tool.get("groups") or []) if str(item).strip()]
            existing = {item.lower() for item in groups}
            added = False
            for cap in caps:
                if cap.lower() not in existing:
                    groups.append(cap)
                    added = True
            tool["groups"] = groups
            tagged += 1 if added else 0
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return tagged, unknown


def main(argv: list[str]) -> int:
    targets = [Path(item) for item in argv[1:]]
    if not targets:
        print("用法：python tools/tag_tool_capabilities.py <TOOL.json> [...]")
        return 2
    for path in targets:
        if not path.exists():
            print(f"跳过（不存在）：{path}")
            continue
        tagged, unknown = tag(path)
        print(f"{path} → 新增能力标签 {tagged} 条")
        if unknown:
            print(f"  未标能力（基础工具，靠 tool_runtime 总开关）：{len(unknown)} 个")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

"""把工具档位写进 TOOL.json 现有字段 `groups`（L0–L4），并拆分 fetch。

用法：
  python -X utf8 tools/tag_tool_catalog.py <TOOL.json 路径> [...]

规则：
- 只往 groups 里追加 `L0`–`L4`，不改其它字段（groups 不参与提示词渲染）；
- `fetch` 拆成两条同名条目：GET 只读 → L1，其余方法/带 body → L3；
- 未登记的工具不自动打标（保持缺省 = L4 fail-closed），并打印出来供人工补。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# 一次性映射：catalog 里的工具名 → 档位（与实施前 tool_permissions.TOOL_LEVELS 等价）
LEVELS: dict[str, str] = {
    # L0 只读 / 对用户输出 / 技能文档
    "echo": "L0",
    "help": "L0",
    "list-workspace": "L0",
    "dir-list": "L0",
    "file-read": "L0",
    "file-tail": "L0",
    "file-search": "L0",
    "get-image-url-from-local": "L0",
    "image-view": "L0",
    "viking-search": "L0",
    "web-search": "L0",
    "clawhub-search": "L0",
    "clawhub-list": "L0",
    "process-var-list": "L0",
    "send-message": "L0",
    "send-image-by-url": "L0",
    "skill-list": "L0",
    "skill-list-simple": "L0",
    "skill-abstract": "L0",
    "skill-overview": "L0",
    "skill-manual": "L0",
    "tool-blacklist": "L0",
    # L1 只影响本人
    "file-write": "L1",
    "file-append": "L1",
    "file-copy": "L1",
    "file-move": "L1",
    "file-upload": "L1",
    "download": "L1",
    "unzip": "L1",
    "process-write": "L1",
    "process-remove": "L1",
    "process-modify": "L1",
    "process-var-remove": "L1",
    "process-init": "L1",
    "reply-image": "L1",
    # L2 全局/跨用户配置
    "worldinfo-write": "L2",
    "add-skill-to-viking": "L2",
    "skill-delete": "L2",
    "clawhub-install": "L2",
    # L3 容器/外部 VM 内执行与外发
    "shell": "L3",
    "image-gen": "L3",
    "port-expose": "L3",
    "delete-file": "L3",
    # L4 穿透宿主机
    "windows-file-copy": "L4",
    "windows-file-move": "L4",
    "browser": "L4",
    "dsh": "L4",
    "codex": "L4",
}

FETCH_GET = {
    "name": "fetch",
    "args": "url",
    "desc": "读取网页内容（GET，只读）",
    "groups": ["网络", "L1"],
    "notes": [
        "只发 GET/HEAD 请求，不写数据",
        "返回文本内容；需要发送非 GET 请求请用下一条 fetch（写请求）",
    ],
}

FETCH_WRITE = {
    "name": "fetch",
    "args": "url|method|data",
    "desc": "发送非 GET 请求（POST/PUT/PATCH/DELETE，可带 body，属于对外写）",
    "groups": ["网络", "L3"],
    "notes": [
        "method 不填时按 GET；要写数据就显式给方法",
        "body 会原样发出，注意别把敏感内容带出去",
    ],
}


def _ensure_level(groups, level: str):
    current = [str(item) for item in (groups or []) if str(item).strip()]
    if not any(item.upper().startswith("L") and item[1:].isdigit() for item in current):
        current.append(level)
    return current


def tag_catalog(path: Path) -> tuple[int, list[str]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    tagged = 0
    unknown: list[str] = []

    for section in data.get("sections") or []:
        tools = section.get("tools")
        if not isinstance(tools, list):
            continue
        rebuilt: list[dict] = []
        for tool in tools:
            if not isinstance(tool, dict):
                rebuilt.append(tool)
                continue
            name = str(tool.get("name") or "").strip()

            if name == "fetch":
                rebuilt.append(dict(FETCH_GET))
                rebuilt.append(dict(FETCH_WRITE))
                tagged += 2
                continue

            level = LEVELS.get(name)
            if level is None:
                unknown.append(name)
                rebuilt.append(tool)
                continue

            tool["groups"] = _ensure_level(tool.get("groups"), level)
            rebuilt.append(tool)
            tagged += 1
        section["tools"] = rebuilt

    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return tagged, unknown


def main(argv: list[str]) -> int:
    targets = [Path(item) for item in argv[1:]]
    if not targets:
        print("用法：python tools/tag_tool_catalog.py <TOOL.json> [<TOOL.json> ...]")
        return 2
    for path in targets:
        if not path.exists():
            print(f"跳过（不存在）：{path}")
            continue
        tagged, unknown = tag_catalog(path)
        print(f"{path} → 打标 {tagged} 条")
        if unknown:
            print(f"  未登记（保持缺省 L4）：{', '.join(unknown)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

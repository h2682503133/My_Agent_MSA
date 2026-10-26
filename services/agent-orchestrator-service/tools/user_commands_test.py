"""user_commands 自检（只测状态侧：可用列表 / 存在性）。

跑法：`python tools/user_commands_test.py`
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

SERVICE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE_ROOT))

from app import user_commands  # noqa: E402

PASSED = 0
FAILED: list[str] = []

AGENTS = {
    "default": {"main": {}, "talker": {}, "tool": {}},
    "users": {"2195239690": {"main": {}, "custom": {}}},
}


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASSED
    if condition:
        PASSED += 1
        print(f"  ok    {name}")
    else:
        FAILED.append(name)
        print(f"  FAIL  {name}" + (f"   [{detail}]" if detail else ""))


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "agent_list.json"
        path.write_text(json.dumps(AGENTS, ensure_ascii=False), encoding="utf-8")

        names = user_commands.available_agents(path, "2195239690")
        check("包含 default 里的智能体", {"main", "talker", "tool"} <= set(names), str(names))
        check("包含该用户的覆盖智能体", "custom" in names, str(names))

        other = user_commands.available_agents(path, "someone-else")
        check("别的用户看不到 custom", "custom" not in other, str(other))

        check("存在性（有）", user_commands.agent_exists(path, "2195239690", "talker") is True)
        check("存在性（无）", user_commands.agent_exists(path, "2195239690", "nope") is False)
        check("空名字", user_commands.agent_exists(path, "2195239690", "  ") is False)

        broken = Path(tmp) / "bad.json"
        broken.write_text("{not json", encoding="utf-8")
        check("配置损坏 → 空列表不抛", user_commands.available_agents(broken, "u") == [])

        missing = Path(tmp) / "nope.json"
        check("文件不存在 → 空列表", user_commands.available_agents(missing, "u") == [])

    print()
    if FAILED:
        print(f"FAILED {len(FAILED)}: {FAILED}")
        return 1
    print(f"ALL OK — {PASSED} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

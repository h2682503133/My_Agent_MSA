"""统一斜杠命令解析自检（/stop、/agent、/mode 一个入口）。

跑法：`python tools/commands_test.py`
"""

from __future__ import annotations

import sys
from pathlib import Path

SERVICE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE_ROOT))

from app import commands  # noqa: E402

PASSED = 0
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASSED
    if condition:
        PASSED += 1
        print(f"  ok    {name}")
    else:
        FAILED.append(name)
        print(f"  FAIL  {name}" + (f"   [{detail}]" if detail else ""))


def test_stop() -> None:
    cmd = commands.parse("/stop")
    check("/stop 识别", cmd is not None and cmd.is_stop and cmd.arg == "", str(cmd))
    check("／stop 全角", (commands.parse("／stop") or None) is not None)
    check("/停止 别名", commands.parse("/停止").is_stop if commands.parse("/停止") else False)
    check("/STOP 大小写", commands.parse("/STOP").is_stop if commands.parse("/STOP") else False)
    check("/stop 带参数不认", commands.parse("/stop 顺便") is None)
    check("普通聊天含 stop 不认", commands.parse("别 stop 啊") is None)


def test_agent_mode() -> None:
    cmd = commands.parse("/agent")
    check("/agent 识别", cmd is not None and cmd.name == commands.AGENT and cmd.arg == "", str(cmd))
    check("/agent talker", commands.parse("/agent talker").arg == "talker")
    check("/switch talker", commands.parse("/switch talker").arg == "talker")
    check("/智能体 talker", commands.parse("/智能体 talker").arg == "talker")
    check("/切换 talker", commands.parse("/切换 talker").arg == "talker")

    check("/mode 识别", commands.parse("/mode").name == commands.MODE)
    check("/mode eco", commands.parse("/mode eco").arg == "eco")
    check("/模式 eco", commands.parse("/模式 eco").arg == "eco")
    check("／mode 全角", commands.parse("／mode eco").arg == "eco")
    check("/MODE 大小写", commands.parse("/MODE eco").name == commands.MODE)
    check("多余空白", commands.parse("  /mode   eco  ").arg == "eco")


def test_not_command() -> None:
    for text in ("", None, "普通聊天", "今天 /mode 是啥", "/whatever x", "/", "／", "你好/stop"):
        check(f"{text!r} 不算命令", commands.parse(text) is None)
    check("is_command 快捷判定", commands.is_command("/mode eco") is True and commands.is_command("聊天") is False)


def main() -> int:
    print("/stop")
    test_stop()
    print("/agent、/mode")
    test_agent_mode()
    print("非命令")
    test_not_command()

    print()
    if FAILED:
        print(f"FAILED {len(FAILED)}: {FAILED}")
        return 1
    print(f"ALL OK — {PASSED} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

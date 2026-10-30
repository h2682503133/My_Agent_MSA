"""PROCESS clock 模式渲染自检（orchestrator 侧，不依赖 gRPC）。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.context_builder import render_process_items, history_time_stamp  # noqa: E402

FAILS = []
CHECKS = 0


def check(name, cond, extra=""):
    global CHECKS
    CHECKS += 1
    if cond:
        print(f"  ok    {name}")
    else:
        FAILS.append(name)
        print(f"  FAIL  {name} {extra}")


def main():
    items = [
        {"title": "第一次", "content": "内容一", "created_at": "2026-10-30T12:34:56+00:00"},
        {"title": "", "content": "无标题内容", "created_at": "2026-10-31T01:02:03+00:00"},
        {"title": "老条目", "content": "旧内容"},  # 老数据没有 created_at
        "纯字符串条目",
    ]

    print("clock 模式")
    lines = render_process_items(items, "clock")
    check("条数一致", len(lines) == 4, str(len(lines)))
    check("第 1 条带时间戳", lines[0].startswith("1. ["), lines[0])
    check("时间格式 [MM-DD HH:MM]", "] " in lines[0] and lines[0][3:9].count("-") == 1, lines[0])
    check("正文保留", "第一次：内容一" in lines[0], lines[0])
    check("无标题条目正常", "无标题内容" in lines[1], lines[1])
    check("老数据不补时间", lines[2] == "3. 老条目：旧内容", lines[2])
    check("纯字符串条目不受影响", lines[3] == "4. 纯字符串条目", lines[3])

    print("none 模式（不带时间）")
    lines_none = render_process_items(items, "none")
    check("没有任何时间戳", all(not line.startswith(f"{i}. [") for i, line in enumerate(lines_none, 1)), str(lines_none))

    print("turn 模式（本轮只数轮次，不加每条时间）")
    lines_turn = render_process_items(items, "turn")
    check("turn 也不加时间戳", lines_turn[0].startswith("1. 第一次"), lines_turn[0])

    print("时间戳与历史消息同一渲染函数")
    check("history_time_stamp 可用", history_time_stamp(items[0]).startswith("[") and history_time_stamp(items[0]).endswith("] "))
    check("无 created_at 返回空串", history_time_stamp({"title": "x"}) == "")
    check("坏 created_at 返回空串", history_time_stamp({"created_at": "not-a-date"}) == "")

    print()
    if FAILS:
        print(f"FAILED {len(FAILS)}: {FAILS}")
        return 1
    print(f"ALL OK — {CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

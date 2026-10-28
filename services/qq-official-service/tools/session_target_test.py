"""qq-official 会话解析自检：三种前缀 + 私聊/群聊 + 非法输入。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.session_target import parse_session_target  # noqa: E402

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


CASES = [
    ("qq_2195239690", ("private", "2195239690")),
    ("qq_g_12345", ("group", "12345")),
    ("qqofficial_2195239690", ("private", "2195239690")),
    ("qqofficial_4F9ECB5FB52916517EE817E3249C514D", ("private", "4F9ECB5FB52916517EE817E3249C514D")),
    ("qqofficial_g_12345", ("group", "12345")),
    ("qqofficial_", None),
    ("", None),
]


def main():
    print("会话解析")
    for session_id, expect in CASES:
        got = parse_session_target(session_id)
        check(f"{session_id or '(空)'} → {expect}", got == expect, str(got))

    print("关键回归：定时任务的 session 必须能解析")
    got = parse_session_target("qqofficial_2195239690")
    check("定时任务 session 可解析（不再 None）", got is not None, str(got))
    check("解析出私聊目标", got and got[0] == "private", str(got))

    print()
    if FAILS:
        print(f"FAILED {len(FAILS)}: {FAILS}")
        return 1
    print(f"ALL OK — {CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

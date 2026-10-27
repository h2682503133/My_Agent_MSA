"""定时任务「执行时段」自检。

覆盖：
  1. parse_window：普通窗口 / 跨天窗口 / 中文写法 / 非法输入
  2. in_window：边界（含端点）、窗口外、跨天
  3. in_task_window：任务无窗口恒执行；有窗口时按时刻判定
  4. window_to_str：回显格式

运行：python tools/timer_window_test.py
"""
import datetime
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.time_parser import (  # noqa: E402
    catch_up_in_window,
    in_task_window,
    in_window,
    next_window_start,
    parse_window,
    window_to_str,
)

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


def ts(hour, minute=0):
    return datetime.datetime(2026, 10, 28, hour, minute).timestamp()


def main():
    check("普通窗口", parse_window("09:00-22:00") == [32400, 79200], parse_window("09:00-22:00"))
    check("跨天窗口", parse_window("22:00-02:00") == [79200, 7200], parse_window("22:00-02:00"))
    check("中文写法", parse_window("9点-18点") == [32400, 64800], parse_window("9点-18点"))
    check("空/非法 → None", parse_window("") is None and parse_window("乱写") is None)
    check("回显格式", window_to_str([32400, 79200]) == "09:00-22:00", window_to_str([32400, 79200]))

    w = parse_window("09:00-22:00")
    check("窗口内 12:00", in_window(w, ts(12)) is True)
    check("边界 09:00 含端点", in_window(w, ts(9)) is True)
    check("边界 22:00 含端点", in_window(w, ts(22)) is True)
    check("窗口外 08:00", in_window(w, ts(8)) is False)
    check("窗口外 23:00", in_window(w, ts(23)) is False)

    w2 = parse_window("22:00-02:00")
    check("跨天 01:00 在内", in_window(w2, ts(1)) is True)
    check("跨天 03:00 在外", in_window(w2, ts(3)) is False)

    check("无窗口恒执行", in_task_window({}, ts(3)) is True)
    task = {"window": parse_window("09:00-22:00")}
    check("任务窗口内执行", in_task_window(task, ts(12)) is True)
    check("任务窗口外跳过", in_task_window(task, ts(3)) is False)
    check("空窗口值视作不限", in_task_window({"window": None}, ts(3)) is True)

    # ── 时段外策略 realign：锚在时段起点 + 重复偏移 ──
    w3 = parse_window("09:00-22:00")

    def local(ts):
        return datetime.datetime.fromtimestamp(ts).strftime("%H:%M")

    start = next_window_start(ts(23, 10), w3)
    check("窗口起点落在次日 09:00", local(start) == "09:00", local(start))

    # 每天（86400）>= 窗口长度(13h)：退回窗口起点 → 必须落在**次日**
    nxt = catch_up_in_window(ts(23, 10), w3, (86400, 86400))
    expect_day = datetime.datetime(2026, 10, 29, 9, 0).timestamp()
    check("每天 → 次日 09:00（不跳过一天）", abs(nxt - expect_day) < 1,
          datetime.datetime.fromtimestamp(nxt).strftime("%m-%d %H:%M"))

    # 每2小时：09:00 + 2h = 11:00，仍在窗口内 → 保留节奏
    nxt2 = catch_up_in_window(ts(23, 10), w3, (7200, 7200))
    expect2 = datetime.datetime(2026, 10, 29, 11, 0).timestamp()
    check("每2小时 → 次日 11:00（保留节奏）", abs(nxt2 - expect2) < 1,
          datetime.datetime.fromtimestamp(nxt2).strftime("%m-%d %H:%M"))

    # 每30分钟：09:30
    nxt3 = catch_up_in_window(ts(23, 10), w3, (1800, 1800))
    check("每30分钟 → 次日 09:30", local(nxt3) == "09:30", local(nxt3))

    # 跨天窗口 22:00-02:00，从 03:00 起 → 当天 22:00
    w4 = parse_window("22:00-02:00")
    nxt4 = catch_up_in_window(ts(3), w4, (0, 0))
    check("跨天窗口 → 当天 22:00", local(nxt4) == "22:00", local(nxt4))

    # 无窗口 → 原样
    check("无窗口不改", catch_up_in_window(ts(3), None, (600, 600)) == ts(3))
    # 已在窗口内 → 原样（不做任何后移）
    check("已在窗口内不改", catch_up_in_window(ts(12), w3, (7200, 7200)) == ts(12))

    # 结果一定在窗口内
    check("追赶结果必在窗口内", in_window(w3, nxt) and in_window(w3, nxt2) and in_window(w3, nxt3))

    print()
    if FAILS:
        print(f"FAILED {len(FAILS)}: {FAILS}")
        return 1
    print(f"ALL OK — {CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

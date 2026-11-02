"""定时任务：执行时段重排用到的间隔区间（防回归：曾因函数名写错 NameError）。

这个 bug 的后果很严重：任务文件不会被重排 → 每轮扫描（5 秒）都会再次提交，
把调度器打满（CreateTask DEADLINE_EXCEEDED），dashboard 也一起卡。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import time_parser  # noqa: E402

FAILS = []
CHECK = 0


def check(name, cond, extra=""):
    global CHECK
    CHECK += 1
    if cond:
        print(f"  ok    {name}")
    else:
        FAILS.append(name)
        print(f"  FAIL  {name} {extra}")


print("间隔区间读取")
check("interval_range 直接读",
      time_parser.interval_range_of({"type": "interval", "interval_range": [1500, 2100]}) == (1500, 2100))
check("兼容旧 interval_seconds",
      time_parser.interval_range_of({"type": "interval", "interval_seconds": 600}) == (600, 600))
check("缺字段兜底 0",
      time_parser.interval_range_of({"type": "interval"}) == (0, 0))

print("catch_up_in_window 可正常使用")
rng = time_parser.interval_range_of({"type": "interval", "interval_range": [60, 120]})

# 已经窗口内 → 原样返回（函数只负责把窗口外的时刻挪进来）
same = time_parser.catch_up_in_window(1000.0, [0, 86400], rng)
check("窗口内不动", same == 1000.0, str(same))

# 没有窗口 → 原样返回
check("无窗口原样返回", time_parser.catch_up_in_window(1000.0, None, rng) == 1000.0)

# 窗口外 → 挪到下一个窗口内，且不抛异常（原来这里是 NameError 现场）
import time as _time

now = _time.time()
window = [60, 120]          # 每天 00:01-00:02
captured = time_parser.catch_up_in_window(now, window, rng)
check("窗口外会重排", captured > now, f"{captured} <= {now}")
check("重排结果落在窗口内", time_parser.in_window(window, captured), str(captured))

print("源码不再引用未定义名")
src = (ROOT / "app" / "timer_task.py").read_text(encoding="utf-8")
check("没有 _interval_range_of", "_interval_range_of" not in src)
check("已导入 interval_range_of", "interval_range_of," in src)

print()
if FAILS:
    print(f"FAILED {len(FAILS)}: {FAILS}")
    raise SystemExit(1)
print(f"ALL OK — {CHECK} checks passed")

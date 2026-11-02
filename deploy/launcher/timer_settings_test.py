"""launcher：timer 任务目录必须落在数据目录里（不是进程工作目录）。

bug 现象：单体起 timer 服务后日志显示 task_dir=roaming\tasks（相对路径），
任务文件落到 CWD 下，用户找不到；集群靠容器 cwd=/app 蒙对了，单体没有。
"""
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deploy"))

from launcher.plan import build  # noqa: E402

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


def make_data_dir(timer=None) -> Path:
    d = Path(tempfile.mkdtemp())
    (d / "config").mkdir(parents=True, exist_ok=True)
    cfg = {"version": 1}
    if timer is not None:
        cfg["timer"] = timer
    (d / "config" / "launcher.json").write_text(
        json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    return d


print("默认落盘位置")
d1 = make_data_dir()
plan1 = build(ROOT, d1, ["core", "timer"], {})
expected = str(d1 / "timer-tasks")
check("TIMER_TASK_DIR 默认在数据目录下", plan1.env.get("TIMER_TASK_DIR") == expected,
      f"got={plan1.env.get('TIMER_TASK_DIR')} want={expected}")
check("不再是相对路径 roaming/tasks",
      not str(plan1.env.get("TIMER_TASK_DIR", "")).replace("\\", "/").startswith("roaming"))

print("launcher.json 覆盖")
d2 = make_data_dir({"dir": "D:/elsewhere/tasks", "scan_fast": 3, "scan_slow": 30})
plan2 = build(ROOT, d2, ["core", "timer"], {})
check("dir 覆盖生效", plan2.env.get("TIMER_TASK_DIR") == "D:/elsewhere/tasks",
      str(plan2.env.get("TIMER_TASK_DIR")))
check("scan_fast 覆盖生效", plan2.env.get("TIMER_SCAN_INTERVAL_FAST") == "3")
check("scan_slow 覆盖生效", plan2.env.get("TIMER_SCAN_INTERVAL_SLOW") == "30")

print("未启用 timer 时不注入")
d3 = make_data_dir()
plan3 = build(ROOT, d3, ["core"], {})
check("没 timer 服务就没有该变量", "TIMER_TASK_DIR" not in plan3.env)
check("也没起 timer 服务", all(s.id != "timer-task-service" for s in plan3.services))

print()
if FAILS:
    print(f"FAILED {len(FAILS)}: {FAILS}")
    raise SystemExit(1)
print(f"ALL OK — {CHECK} checks passed")

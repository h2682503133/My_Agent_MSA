"""执行时段（第 6 参）端到端接线自检。

历史 bug：解析层能拿到 `window_str`（如 08:00-02:00），但编排器创建定时任务时
没把它传给定时服务 → 任务文件里 `window` 永远是 null → 执行区间形同虚设。
本测试同时验证：
1. 解析层能正确取出第 6 参（含跨天窗口）；
2. 编排器调用处确实把 `window_str` 传下去了（静态检查，防止再次漏掉）；
3. 定时服务对"一次性任务 + 时段外"会顺延到下一个时段起点，而不是静默丢弃。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
ORCH = ROOT / "services" / "agent-orchestrator-service" / "app"
TIMER = ROOT / "services" / "timer-task-service" / "app"

# app 包在服务根目录下；time_parser 在 app/ 里（同名包会冲突，先加载纯函数模块）
sys.path.insert(0, str(ORCH.parent))
sys.path.insert(0, str(TIMER))

import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location("timer_time_parser", TIMER / "time_parser.py")
tp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tp)  # type: ignore[union-attr]
in_window, next_window_start, parse_window = tp.in_window, tp.next_window_start, tp.parse_window

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


def window_of(line: str) -> str:
    block = sp._find_command_block(line, "定时任务")
    parts = [p.strip() for p in (block or "").split("|")]
    rest = parts[1:]
    return rest[4] if len(rest) > 4 else ""


def main() -> int:
    print("① 解析层：第 6 参")
    check("普通窗口", window_of("定时任务:submit_task|main|内容|03:00:00|每25分钟-55分钟|08:00-02:00")
          == "08:00-02:00", window_of("定时任务:submit_task|main|内容|03:00:00|每25分钟-55分钟|08:00-02:00"))
    check("空占位 = 不限制",
          window_of("定时任务:submit_task|main|内容|03:00:00|每25分钟-55分钟|") == "")
    check("只写 5 段时没有窗口",
          window_of("定时任务:submit_task|main|内容|03:00:00|每25分钟-55分钟") == "")
    check("send_message 同样支持",
          window_of("定时任务:send_message|main|内容|10:00|每天|09:00-18:00") == "09:00-18:00")

    print("② 窗口语义（跨天）")
    nine_to_six = parse_window("09:00-18:00")
    late = parse_window("22:00-02:00")
    check("解析 09:00-18:00", nine_to_six == [32400, 64800], str(nine_to_six))
    check("解析 22:00-02:00（跨天）", late == [79200, 7200], str(late))
    check("08:00-02:00 也是跨天", parse_window("08:00-02:00") == [28800, 7200],
          str(parse_window("08:00-02:00")))

    # 用固定时间戳验证 in_window（本地时区）
    import time as _t

    def ts(h: int, m: int = 0) -> float:
        now = _t.localtime()
        return _t.mktime((now.tm_year, now.tm_mon, now.tm_mday, h, m, 0, 0, 0, -1))

    win = parse_window("08:00-02:00")
    check("03:00 在 08:00-02:00 之外", in_window(win, ts(3)) is False)
    check("09:00 在内", in_window(win, ts(9)) is True)
    check("01:30 在内（跨天生效）", in_window(win, ts(1, 30)) is True)
    check("02:00 边界仍算在内", in_window(win, ts(2)) is True)

    nxt = next_window_start(ts(3), win)
    check("03:00 的下一个时段起点是今天 08:00",
          _t.localtime(nxt).tm_hour == 8 and _t.localtime(nxt).tm_mday == _t.localtime(ts(3)).tm_mday,
          f"{_t.strftime('%m-%d %H:%M', _t.localtime(nxt))}")

    print("③ 接线：编排器必须把 window_str 传给定时服务")
    runtime_src = (ORCH / "agent_runtime.py").read_text(encoding="utf-8")
    idx = runtime_src.find("client.create_timer_task(")
    check("找到 create_timer_task 调用", idx != -1)
    call_snippet = runtime_src[idx: idx + 1200] if idx != -1 else ""
    check("调用里带 window_str=timer.get(...)",
          "window_str=timer.get(" in call_snippet,
          call_snippet[:240])

    client_src = (ORCH / "timer_task_client.py").read_text(encoding="utf-8")
    check("客户端方法签名含 window_str", "window_str" in client_src)
    check("客户端请求体带 window_str", "window_str=window_str" in client_src)

    service_src = (TIMER / "timer_task.py").read_text(encoding="utf-8")
    check("定时服务创建时解析 window", "parse_window(window_str)" in service_src)
    check("定时服务写入 window 字段", '"window": window' in service_src)
    check("一次性任务时段外会顺延", "next_window_start(now, task.get(\"window\"))" in service_src,
          "顺延逻辑缺失")
    check("顺延后 continue（不落删除分支）",
          bool(re.search(r"顺延到时段起点[\s\S]{0,400}?continue", service_src)))

    print()
    if FAILS:
        print(f"FAILED {len(FAILS)}: {FAILS}")
        return 1
    print(f"ALL OK — {CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

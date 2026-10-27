"""定时任务渠道策略自检（新默认）：qqofficial 不再被屏蔽。

覆盖：
  1. 未设 SCHEDULER_EXCLUDED_CHANNELS 时，默认屏蔽集合为空
  2. timer_task 事件不带 suppress_channels
  3. 用户只绑了 qqofficial 时，定时事件能投到 qqofficial 订阅者
  4. 显式设 env 后仍能收回（机制没坏）

运行：python tools/scheduled_channel_default_smoke_test.py
"""
import os
import queue
import sys
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVICE_DIR))

os.environ.pop("SCHEDULER_EXCLUDED_CHANNELS", None)

from app import config  # noqa: E402
from app.event_bus import EventBus, suppressed_channels, task_event  # noqa: E402
from app.scheduled_task import DeliveryTarget, ScheduledTask  # noqa: E402

FAILURES = []
CHECKS = 0


def check(name, condition, extra=""):
    global CHECKS
    CHECKS += 1
    if not condition:
        FAILURES.append(name)
        print(f"  FAIL  {name} {extra}")
    else:
        print(f"  ok    {name}")


class FakeUserClient:
    def __init__(self, table):
        self.table = table

    def get_channel_targets(self, user_id):
        return list(self.table.get(user_id, []))


def make_bus(table):
    bus = EventBus()
    bus.set_user_client(FakeUserClient(table))
    return bus


def drain(q, limit=5):
    items = []
    for _ in range(limit):
        try:
            items.append(q.get_nowait())
        except queue.Empty:
            break
    return items


def main():
    check("默认屏蔽集合为空", config.SCHEDULED_EXCLUDED_CHANNELS == set(),
          str(config.SCHEDULED_EXCLUDED_CHANNELS))

    task = ScheduledTask(
        task_id="t1",
        user_id="u1",
        session_id="qq_u1",
        channel="qqofficial",
        content="定时提醒喝水",
        metadata={"source": "timer_task"},
    )
    event = task_event(task, "assistant_message", text="提醒：喝水")
    check("timer 事件不带 suppress_channels", not suppressed_channels(event),
          str(event.get("metadata")))

    # 用户只绑了 qqofficial，且它在线
    bus = make_bus({"u1": [("qqofficial", "OPENID1", 0)]})
    q = queue.Queue()
    bus._subscribers["qq-official-1"] = ({"qqofficial"}, q)
    bus.publish(event)
    got = drain(q)
    check("定时事件投到 qqofficial", len(got) == 1, f"got={len(got)}")
    if got:
        check("投递渠道正确", got[0].get("channel") == "qqofficial", str(got[0].get("channel")))
        check("直投不改写身份", got[0].get("user_id") == "u1", str(got[0].get("user_id")))

    # llbot(qq) 掉线 → 定时事件转投 qqofficial（跨渠道才改写身份）
    bus2 = make_bus({"u1": [("qq", "2195239690", 0), ("qqofficial", "OPENID1", 1)]})
    q2 = queue.Queue()
    bus2._subscribers["qq-official-1"] = ({"qqofficial"}, q2)
    task_qq = ScheduledTask(
        task_id="t2",
        user_id="u1",
        session_id="qq_u1",
        channel="qq",
        content="定时提醒喝水",
        metadata={"source": "timer_task"},
    )
    bus2.publish(task_event(task_qq, "assistant_message", text="提醒：喝水"))
    got2 = drain(q2)
    check("llbot 掉线后转投 qqofficial", len(got2) == 1, f"got={len(got2)}")
    if got2:
        check("转投渠道正确", got2[0].get("channel") == "qqofficial", str(got2[0].get("channel")))
        check("转投身份改写为 openid", got2[0].get("user_id") == "OPENID1",
              str(got2[0].get("user_id")))
        check("记录了转投来源", str((got2[0].get("metadata") or {}).get("fallback_from") or "") != "" or
              str((got2[0].get("metadata") or {})) != "{}",
              str(got2[0].get("metadata")))

    # 显式收回
    config.SCHEDULED_EXCLUDED_CHANNELS = {"qqofficial"}
    event2 = task_event(task, "assistant_message", text="提醒：喝水")
    check("显式收回后仍会打屏蔽标记", "qqofficial" in suppressed_channels(event2),
          str(suppressed_channels(event2)))

    print()
    if FAILURES:
        print(f"FAILED {len(FAILURES)}: {FAILURES}")
        return 1
    print(f"ALL OK — {CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

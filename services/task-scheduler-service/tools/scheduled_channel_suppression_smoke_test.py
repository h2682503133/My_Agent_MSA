"""定时任务渠道屏蔽自测：定时结果不能落进 qqofficial（主动消息受限）。

覆盖：
  1. task_event 给 timer_task 来源的事件打上 suppress_channels=qqofficial
  2. publish 遇到被屏蔽的原始渠道 → 不投给该渠道订阅者，改投其它渠道
  3. 转投链路里被屏蔽渠道永远不会被选中
  4. 非定时任务的事件不受影响（仍按原渠道投递）

运行：python tools/scheduled_channel_suppression_smoke_test.py
"""
import os
import queue
import sys
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVICE_DIR))

os.environ.setdefault("SCHEDULER_EXCLUDED_CHANNELS", "qqofficial")

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
    def __init__(self, table=None):
        self.table = table or {}

    def get_channel_targets(self, user_id):
        return list(self.table.get(user_id, []))


def make_bus(table=None):
    bus = EventBus()
    bus.set_user_client(FakeUserClient(table))
    return bus


def subscribe(bus, subscriber_id, channels):
    q: queue.Queue = queue.Queue(maxsize=50)
    bus._subscribers[subscriber_id] = (set(channels), q)
    return q


def make_task(source_timer: bool):
    meta = {"source": "timer_task"} if source_timer else {}
    return ScheduledTask(
        task_id="task-1",
        user_id="2195239690",
        session_id="qq_2195239690",
        channel="qqofficial",
        content="该喝水了",
        client_message_id="m1",
        delivery_target=DeliveryTarget(
            channel="qqofficial", user_id="2195239690", conversation_id="qq_2195239690"
        ),
        metadata=meta,
    )


def event_of(task):
    return task_event(task, "assistant_message", text="该喝水了",
                      metadata={"visible_to_user": "true", "final": "true"})


print("[1] task_event 打标记")
timer_event = event_of(make_task(True))
plain_event = event_of(make_task(False))
check("定时任务事件带有 qqofficial 屏蔽", "qqofficial" in suppressed_channels(timer_event),
      suppressed_channels(timer_event))
check("普通事件不带屏蔽", suppressed_channels(plain_event) == set(), suppressed_channels(plain_event))
check("原 metadata 保留", timer_event["metadata"].get("visible_to_user") == "true")

print("[2] publish 跳过被屏蔽渠道")
bus = make_bus({"2195239690": [("qq", "2195239690", 0), ("web", "h268", 1)]})
official_q = subscribe(bus, "sub-official", ["qqofficial"])
web_q = subscribe(bus, "sub-web", ["web"])
bus.publish(timer_event)
check("qqofficial 队列为空（被屏蔽）", official_q.empty())
check("改投到 web", not web_q.empty(), f"web_q.empty={web_q.empty()}")
if not web_q.empty():
    forwarded = web_q.get_nowait()
    check("转投后 channel=web", forwarded["channel"] == "web", forwarded["channel"])
    check("转投后 user_id 换成 web 侧 id", forwarded["user_id"] == "h268", forwarded["user_id"])
    check("转投后 session 重建", forwarded["session_id"] == "web_h268", forwarded["session_id"])

print("[3] 转投链路不选被屏蔽渠道")
bus2 = make_bus({"2195239690": [("qqofficial", "4F9E", 0), ("web", "h268", 1)]})
official_q2 = subscribe(bus2, "sub-official", ["qqofficial"])
web_q2 = subscribe(bus2, "sub-web", ["web"])
# 原始渠道是 llbot 的 qq（没有订阅者）→ 走转投，必须跳过 qqofficial
bus2.publish({**timer_event, "channel": "qq", "session_id": "qq_2195239690"})
check("转投没有落到 qqofficial", official_q2.empty())
check("转投落到 web", not web_q2.empty())

print("[4] 非定时任务不受影响")
bus3 = make_bus({"2195239690": [("qq", "2195239690", 0)]})
official_q3 = subscribe(bus3, "sub-official", ["qqofficial"])
bus3.publish(plain_event)
check("普通事件仍投给 qqofficial", not official_q3.empty())

print()
if FAILURES:
    print(f"FAILED: {len(FAILURES)}/{CHECKS}")
    for name in FAILURES:
        print("  -", name)
    sys.exit(1)
print(f"ALL OK - {CHECKS} checks")

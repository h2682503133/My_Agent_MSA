"""统一 qq 渠道 · 自检（不依赖 gRPC；订阅用后台线程驱动，因为 subscribe 是生成器）。"""

import queue
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import event_bus as eb  # noqa: E402

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


class Consumer:
    """订阅生成器 → 后台线程消费，收到的事件进队列。"""

    def __init__(self, bus, subscriber_id, channels, priority):
        self.events: queue.Queue = queue.Queue()
        self.gen = bus.subscribe(subscriber_id, channels, priority=priority)
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        for event in self.gen:
            if event is not None:
                self.events.put(event)

    def get(self, timeout=1.0):
        try:
            return self.events.get(timeout=timeout)
        except queue.Empty:
            return None

    def close(self):
        try:
            self.gen.close()
        except Exception:
            pass


def settle(seconds=0.25):
    time.sleep(seconds)


def main():
    print("渠道/会话归一（与 timer 服务同口径）")
    aliases = {"qq", "qqofficial", "qq_llbot", "qq-llbot", "qqo", "llbot"}

    def normalize_channel(channel_id):
        key = str(channel_id or "").strip()
        return "qq" if key.lower() in aliases else key

    def normalize_session(session_id, user_id):
        raw = str(session_id or "").strip()
        if not raw:
            return f"qq_{user_id}" if user_id else ""
        prefix, _, rest = raw.partition("_")
        if prefix.lower() in aliases and rest == str(user_id or ""):
            return f"qq_{user_id}"
        return raw

    check("qqofficial → qq", normalize_channel("qqofficial") == "qq")
    check("web 不动", normalize_channel("web") == "web")
    check("qqofficial_1 → qq_1", normalize_session("qqofficial_1", "1") == "qq_1")
    check("群聊不动", normalize_session("qq_g_9", "1") == "qq_g_9")

    print("同渠道多订阅者：按优先级只投一个")
    bus = eb.EventBus()
    high = Consumer(bus, "qq-llbot-1", ["qq"], 100)
    low = Consumer(bus, "qq-official-1", ["qq"], 50)
    settle()

    bus.publish({"event_id": "e1", "channel": "qq", "user_id": "u1", "type": "assistant_message"})
    got_high = high.get()
    check("高优先级收到", got_high is not None)
    check("低优先级没收到（只投一个）", low.get(timeout=0.5) is None)

    print("ack 失败 → 自动切另一个后端")
    bus = eb.EventBus()
    high = Consumer(bus, "qq-llbot-1", ["qq"], 100)
    low = Consumer(bus, "qq-official-1", ["qq"], 50)
    settle()
    bus.publish({
        "event_id": "e2", "channel": "qq", "user_id": "u1",
        "type": "assistant_message", "text": "hi",
        "metadata": {"visible_to_user": "true", "_ack_required": "1"},
    })
    check("第一次投给高优先级", high.get() is not None)
    disposition = bus.ack("e2", ok=False, error="llbot offline")
    check("disposition 表示切换后端", str(disposition[1]).startswith("switched:"), str(disposition))
    check("低优先级收到重投", low.get() is not None)

    print("两个都失败 → 缓冲（无 user_client 时）")
    bus = eb.EventBus()
    high = Consumer(bus, "qq-llbot-1", ["qq"], 100)
    low = Consumer(bus, "qq-official-1", ["qq"], 50)
    settle()
    bus.publish({
        "event_id": "e3", "channel": "qq", "user_id": "u2",
        "type": "assistant_message", "text": "hi",
        "metadata": {"visible_to_user": "true", "_ack_required": "1"},
    })
    high.get()
    bus.ack("e3", ok=False, error="fail1")
    low.get()
    disposition2 = bus.ack("e3", ok=False, error="fail2")
    check("最终落到 buffered", "buffered" in str(disposition2), str(disposition2))

    print("优先级数值生效（与注册顺序无关）")
    bus = eb.EventBus()
    low = Consumer(bus, "qq-official-1", ["qq"], 10)
    high = Consumer(bus, "qq-llbot-1", ["qq"], 90)
    settle()
    bus.publish({
        "event_id": "e4", "channel": "qq", "user_id": "u3",
        "type": "assistant_message", "metadata": {"visible_to_user": "true", "_ack_required": "1"},
    })
    check("数值大的先收到（即便后注册）", high.get() is not None)
    check("数值小的没收到", low.get(timeout=0.5) is None)

    for consumer in (high, low):
        consumer.close()

    print()
    if FAILS:
        print(f"FAILED {len(FAILS)}: {FAILS}")
        return 1
    print(f"ALL OK — {CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

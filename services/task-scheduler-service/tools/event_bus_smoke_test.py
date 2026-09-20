"""事件投递状态机冒烟测试（ack / 跨渠道转投 / 缓冲 / 回放 / 超时）。

核心断言：投递失败不再被当成成功 —— 这是「QQ 离线时消息完全没反应」的根因。

运行：python tools/event_bus_smoke_test.py
"""

import os
import queue
import sys
import time
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVICE_DIR))

# 让 ack 超时足够短，以便在测试里真的触发超时分支（模块在 import 时读环境变量）
os.environ["ACK_TIMEOUT_SECONDS"] = "0.3"
os.environ["MAX_FALLBACK_HOPS"] = "3"
os.environ["MAX_BUFFER_ATTEMPTS"] = "2"

from app.event_bus import (  # noqa: E402
    ACK_TIMEOUT_SECONDS,
    EventBus,
    is_ack_required,
)

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
    """可编程的渠道表：user_id -> [(channel, channel_user_id, priority)]"""

    def __init__(self, table=None):
        self.table = table or {}

    def get_channel_targets(self, user_id):
        return list(self.table.get(user_id, []))


def make_bus(table=None):
    bus = EventBus()
    bus.set_user_client(FakeUserClient(table))
    return bus


def subscribe_fake(bus, subscriber_id, channels):
    """直接注册订阅者并返回其队列（跳过生成器，测试 publish/ack 逻辑）。"""
    q: queue.Queue = queue.Queue(maxsize=500)
    bus._subscribers[subscriber_id] = (set(channels), q)
    return q


def drain(q):
    out = []
    while True:
        try:
            out.append(q.get_nowait())
        except queue.Empty:
            return out


def ev(event_id, channel, user_id, etype="assistant_message", visible="true", **meta):
    return {
        "event_id": event_id,
        "task_id": "task-1",
        "user_id": user_id,
        "session_id": f"{channel}_{user_id}",
        "channel": channel,
        "type": etype,
        "text": "hello",
        "images": [],
        "waiting": 0,
        "error": "",
        "delivery_target": {},
        "metadata": {**({"visible_to_user": visible} if visible is not None else {}), **meta},
    }


print("=== 1. 哪些事件需要 ack ===")
check("assistant_message + visible_to_user=true → 需要",
      is_ack_required(ev("e1", "web", "u", visible="true")) is True)
check("assistant_message 无 visible_to_user → 不需要",
      is_ack_required(ev("e2", "web", "u", visible=None)) is False)
check("task_started → 不需要", is_ack_required(ev("e3", "web", "u", etype="task_started")) is False)
check("task_completed → 不需要", is_ack_required(ev("e4", "web", "u", etype="task_completed")) is False)
check("task_failed → 需要", is_ack_required(ev("e5", "web", "u", etype="task_failed")) is True)
check("task_waiting_user → 需要", is_ack_required(ev("e6", "web", "u", etype="task_waiting_user")) is True)
check("task_timeout → 需要", is_ack_required(ev("e7", "web", "u", etype="task_timeout")) is True)
check("visible_to_user=TRUE 大小写容错",
      is_ack_required(ev("e8", "web", "u", visible="TRUE")) is True)

print("\n=== 2. 投递成功路径 ===")
bus = make_bus()
q = subscribe_fake(bus, "web-gateway-1", ["web"])
bus.publish(ev("e1", "web", "u1"))
got = drain(q)
check("事件已入队", len(got) == 1 and got[0]["event_id"] == "e1")
check("需要确认的事件被打了 _ack_required 标记",
      got and got[0]["metadata"].get("_ack_required") == "1")
check("在途表登记了该事件", bus.inflight_stats()["inflight"] == 1)
accepted, disposition = bus.ack("e1", ok=True)
check("ack ok 被接受", accepted is True and disposition == "acked")
check("ack 后在途表清空", bus.inflight_stats()["inflight"] == 0)

print("\n=== 3. ack 未知事件 ===")
accepted, disposition = bus.ack("nonexistent", ok=False)
check("未知事件 ack 返回 accepted=False", accepted is False and disposition == "unknown")

print("\n=== 4. 过程类事件不登记在途（fire-and-forget）===")
bus = make_bus()
q = subscribe_fake(bus, "web-gateway-1", ["web"])
bus.publish(ev("p1", "web", "u1", etype="task_started"))
drain(q)
check("task_started 不进在途表", bus.inflight_stats()["inflight"] == 0)

print("\n=== 5. 投递失败 → 跨渠道转投（并改写 user_id 为渠道身份）===")
# 规范用户 h268 绑了 qq(2195239690) 和 web(h268)；QQ 号是它的渠道身份（反查后）
TABLE = {
    "2195239690": [("qq", "2195239690", 0), ("web", "h268", 0)],
    "h268": [("qq", "2195239690", 0), ("web", "h268", 0)],
}
bus = make_bus(TABLE)
q_qq = subscribe_fake(bus, "qq-llbot-1", ["qq"])
q_web = subscribe_fake(bus, "web-gateway-1", ["web"])

# QQ 事件投给 qq 订阅者，qq 端回报失败
bus.publish(ev("e10", "qq", "2195239690"))
drain(q_qq)
check("qq 事件先投给 qq 订阅者", bus.inflight_stats()["inflight"] == 1)
accepted, disposition = bus.ack("e10", ok=False, error="satori offline")
check("失败后转投 web 渠道", disposition == "forwarded:web", f"got={disposition}")
got_web = drain(q_web)
check("web 订阅者收到该事件", len(got_web) == 1 and got_web[0]["event_id"] == "e10")
check("**user_id 已改写为 web 侧身份 h268**",
      got_web and got_web[0]["user_id"] == "h268", f"got={got_web[0]['user_id'] if got_web else None}")
check("channel 已改写为 web", got_web and got_web[0]["channel"] == "web")
check("**session_id 已按目标渠道重建（web_h268）**",
      got_web and got_web[0]["session_id"] == "web_h268",
      f"got={got_web[0]['session_id'] if got_web else None}")
check("标记了来源渠道与跳数",
      got_web and got_web[0]["metadata"].get("_fallback_from") == "qq"
      and got_web[0]["metadata"].get("_fallback_hops") == "1")
check("转投后重新登记在途（可继续级联）", bus.inflight_stats()["inflight"] == 1)

print("\n=== 6. 转投目标再失败 → 缓冲 ===")
accepted, disposition = bus.ack("e10", ok=False, error="no online sse")
check("二级失败后进入缓冲", disposition == "buffered", f"got={disposition}")
check("缓冲中记录了该用户", bus.inflight_stats()["buffered_users"] == 1)
check("缓冲计数为 1", bus.inflight_stats()["buffered_total"] == 1)

print("\n=== 7. 无订阅者 → 直接转投 ===")
bus = make_bus(TABLE)
q_web = subscribe_fake(bus, "web-gateway-1", ["web"])   # 只有 web 订阅者
bus.publish(ev("e11", "qq", "2195239690"))              # qq 渠道无人订阅
got_web = drain(q_web)
check("无 qq 订阅者时直接转投 web", len(got_web) == 1 and got_web[0]["event_id"] == "e11")
check("转投后 user_id 为 h268", got_web and got_web[0]["user_id"] == "h268")

print("\n=== 8. 全部渠道都失败 → 缓冲（不丢）===")
bus = make_bus(TABLE)
q_qq = subscribe_fake(bus, "qq-llbot-1", ["qq"])
q_web = subscribe_fake(bus, "web-gateway-1", ["web"])
bus.publish(ev("e12", "qq", "2195239690"))
drain(q_qq)
bus.ack("e12", ok=False)
drain(q_web)
accepted, disposition = bus.ack("e12", ok=False)
check("两跳都失败后缓冲", disposition == "buffered", f"got={disposition}")

print("\n=== 9. 缓冲回放：两条真实触发路径 ===")
# 注意缓冲的键是**最后尝试的渠道身份**：e12 转投到 web 时 user_id 被改写成 h268，
# 所以缓冲键是 h268 而不是 QQ 号。因为别名解析让两者解析出同一组渠道，两条 flush 路径都能命中。
check("缓冲键为转投后的渠道身份", bus.inflight_stats()["buffered_total"] == 1)

# 路径 A：Satori 重连 → 按渠道 flush（不带 user_id）
flushed = bus.flush_buffered(channel="qq")
got_qq = drain(q_qq)
check("按渠道 flush 补发到 qq 订阅者", flushed == 1 and len(got_qq) == 1, f"flushed={flushed}")
check("补发的仍是同一条事件", got_qq and got_qq[0]["event_id"] == "e12")
check("user_id 被改写回 QQ 身份", got_qq and got_qq[0]["user_id"] == "2195239690",
      f"got={got_qq[0]['user_id'] if got_qq else None}")
check("session_id 按 qq 渠道重建（qq_2195239690）",
      got_qq and got_qq[0]["session_id"] == "qq_2195239690",
      f"got={got_qq[0]['session_id'] if got_qq else None}")
check("补发后缓冲清空", bus.inflight_stats()["buffered_total"] == 0)

# 路径 B：web 用户 SSE 上线 → 按 user_id + 渠道 flush
bus2 = make_bus(TABLE)
q_qq2 = subscribe_fake(bus2, "qq-llbot-1", ["qq"])
q_web2 = subscribe_fake(bus2, "web-gateway-1", ["web"])
bus2.publish(ev("e30", "web", "h268"))
drain(q_web2)
bus2.ack("e30", ok=False)      # web 失败 → 转投 qq
drain(q_qq2)
bus2.ack("e30", ok=False)      # qq 也失败 → 缓冲
check("两条渠道都失败后缓冲", bus2.inflight_stats()["buffered_total"] == 1)
flushed2 = bus2.flush_buffered(user_id="h268", channel="web")
got_web2 = drain(q_web2)
check("按 user_id+渠道 flush 补发到 web 订阅者", flushed2 == 1 and len(got_web2) == 1,
      f"flushed={flushed2}")
check("补发到 web 时 user_id 为 h268", got_web2 and got_web2[0]["user_id"] == "h268")

print("\n=== 10. flush 对不可达渠道不丢失（放回缓冲）===")
bus = make_bus({"solo": [("web", "solo", 0)]})   # 没有 qq 渠道
q_web = subscribe_fake(bus, "web-gateway-1", ["web"])
event = ev("e13", "web", "solo")
bus.publish(event)
drain(q_web)
bus.ack("e13", ok=False)      # 无其它渠道 → 缓冲
check("无其它渠道时缓冲", bus.inflight_stats()["buffered_total"] == 1)
flushed = bus.flush_buffered(user_id="solo", channel="qq")   # 用户没有 qq 渠道
check("请求不存在的渠道不补发", flushed == 0)
check("事件仍留在缓冲里（未丢失）", bus.inflight_stats()["buffered_total"] == 1)

print("\n=== 11. 缓冲尝试上限（防止 回放→失败→再缓冲 的死循环）===")
# MAX_BUFFER_ATTEMPTS=2：同一条事件反复「回放 → 投递失败 → 回到缓冲」，
# 超过上限后必须被丢弃，而不是无限循环下去。
bus = make_bus({"solo": [("web", "solo", 0)]})
q_web = subscribe_fake(bus, "web-gateway-1", ["web"])
bus.publish(ev("loop", "web", "solo"))
drain(q_web)
_a, first = bus.ack("loop", ok=False)
check("第 1 次失败进入缓冲", first == "buffered", f"got={first}")
check("缓冲计 1 条", bus.inflight_stats()["buffered_total"] == 1)

bus.flush_buffered(user_id="solo", channel="web")
drain(q_web)
_a, second = bus.ack("loop", ok=False)
check("第 2 次失败仍在缓冲", second == "buffered", f"got={second}")

bus.flush_buffered(user_id="solo", channel="web")
drain(q_web)
_a, third = bus.ack("loop", ok=False)
check("超过上限后丢弃（不再无限重试）", third == "dropped", f"got={third}")
check("丢弃后缓冲为空", bus.inflight_stats()["buffered_total"] == 0)

print("\n=== 12. ack 超时（消费端崩溃/卡死）===")
check("ACK_TIMEOUT_SECONDS 已被测试缩短", ACK_TIMEOUT_SECONDS == 0.3)
bus = make_bus(TABLE)
q_qq = subscribe_fake(bus, "qq-llbot-1", ["qq"])
q_web = subscribe_fake(bus, "web-gateway-1", ["web"])
bus.start_sweeper()
bus.publish(ev("e20", "qq", "2195239690"))
drain(q_qq)
check("超时前在途表有记录", bus.inflight_stats()["inflight"] == 1)
deadline = time.time() + 3.0
got_web = []
while time.time() < deadline:
    got_web = drain(q_web)
    if got_web:
        break
    time.sleep(0.1)
check("超时后自动转投 web（无需消费端回报）",
      len(got_web) == 1 and got_web[0]["event_id"] == "e20", f"got={got_web}")

print("\n=== 13. 订阅者排除（不把失败事件弹回原订阅者）===")
bus = make_bus(TABLE)
q_qq = subscribe_fake(bus, "qq-llbot-1", ["qq"])
q_web = subscribe_fake(bus, "web-gateway-1", ["web"])
bus.publish(ev("e21", "qq", "2195239690"))
drain(q_qq)
bus.ack("e21", ok=False)
check("原订阅者没有收到回弹", len(drain(q_qq)) == 0)
check("另一个订阅者收到", len(drain(q_web)) == 1)

print("\n=== 14. 用户无渠道（未绑定的 QQ 用户）===")
bus = make_bus({})   # 2682503133 这类没有别名、没有绑定的用户
q_qq = subscribe_fake(bus, "qq-llbot-1", ["qq"])
bus.publish(ev("e22", "qq", "2682503133"))
drain(q_qq)
accepted, disposition = bus.ack("e22", ok=False)
check("无其它渠道可用时缓冲", disposition == "buffered", f"got={disposition}")

print("\n=== 15. 无 user_id 的事件不参与转投 ===")
bus = make_bus(TABLE)
accepted, disposition = bus.ack("missing", ok=False)
check("无 user_id 时 publish 不抛异常", True)
bus.publish(ev("e23", "qq", ""))
check("空 user_id 事件被跳过", bus.inflight_stats()["buffered_total"] == 0)

print()
if FAILURES:
    print(f"FAILURES ({len(FAILURES)}):")
    for name in FAILURES:
        print(f"  - {name}")
    sys.exit(1)
print(f"ALL OK — {CHECKS} checks passed")

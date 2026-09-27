#!/usr/bin/env python3
"""`/stop` 打断指令 —— task-scheduler-service 侧本地冒烟测试。

覆盖的是调度器这一侧的判定与副作用，全部与网络解耦（orchestrator 用替身、
事件总线用记录器）：

  1. 指令识别：只认精确的 /stop（含全角斜杠、大小写、前后空白）
  2. 回执分工：打断成功时不自己发消息（由被打断任务的暂停事件给出回执），
     只有「没东西可停」和「请求失败」才补说明
  3. 在 CreateTask 入口拦截：不排队、不登记幂等（对照组证明普通消息不受影响）
  4. 队列竞态消化：/stop 落地前已排上队列的消息被升级成插话，不会两边互等

用法：python tools/stop_command_smoke_test.py
"""

import queue
import sys
import time
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parents[1]
if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))

from app import scheduler  # noqa: E402
from app.orchestrator_client import StopResult  # noqa: E402
from app.scheduled_task import DeliveryTarget, ScheduledTask  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASSED.append(name)
        print(f"  [PASS] {name}")
    else:
        FAILED.append(name)
        print(f"  [FAIL] {name}{(' -> ' + detail) if detail else ''}")


class FakeBus:
    """事件总线替身：只记录，避免真实 publish 去走转投/缓冲链路。"""

    def __init__(self) -> None:
        self.events: list[dict] = []

    def publish(self, event) -> None:
        self.events.append(event)

    def last_text(self) -> str:
        return self.events[-1].get("text", "") if self.events else ""


class FakeOrchestrator:
    """orchestrator 替身：只实现 request_stop。"""

    def __init__(self, ok: bool = True, stopped: bool = False, message: str = "") -> None:
        self.ok = ok
        self.stopped = stopped
        self.message = message
        self.calls: list[dict] = []

    def request_stop(self, user_id: str, task_id: str = "", reason: str = "") -> StopResult:
        self.calls.append({"user_id": user_id, "task_id": task_id, "reason": reason})
        return StopResult(ok=self.ok, stopped=self.stopped, message=self.message)


BUS = FakeBus()
scheduler.event_bus = BUS


def make_task(
    user_id: str = "u1",
    content: str = "/stop",
    channel: str = "web",
    task_id: str = "task-stop-1",
    client_message_id: str = "msg-1",
) -> ScheduledTask:
    session_id = f"{channel}_{user_id}"
    return ScheduledTask(
        task_id=task_id,
        user_id=user_id,
        session_id=session_id,
        channel=channel,
        content=content,
        client_message_id=client_message_id,
        delivery_target=DeliveryTarget(
            channel=channel,
            user_id=user_id,
            conversation_id=session_id,
            reply_to=client_message_id,
        ),
    )


def reset(orch: FakeOrchestrator) -> None:
    scheduler._ORCHESTRATOR_CLIENT = orch
    with scheduler.USER_LOCK:
        scheduler.USER_QUEUES.clear()
    with scheduler.SUSPENDED_LOCK:
        scheduler.SUSPENDED_TASKS.clear()
    with scheduler.BUSY_LOCK:
        scheduler.BUSY_USERS.clear()
    with scheduler.IDEMPOTENCY_LOCK:
        scheduler.IDEMPOTENCY_INDEX.clear()
    with scheduler.SLOTS_LOCK:
        scheduler.BATCH_SLOTS[:] = [None] * scheduler.BATCH_SIZE
    scheduler.processed = 0
    BUS.events.clear()


# ══════════════════════════════════════════════════════════════
print("\n[1] 指令识别")
# ══════════════════════════════════════════════════════════════
for text in ["/stop", "/stop ", "  /stop", "/STOP", "/Stop", "/停止", "／stop", "／停止", "/stop\n"]:
    check(f"识别 {text!r}", scheduler.is_stop_command(text) is True)

for text in ["/stop 别做了", "/stopx", "/stops", "停止", "stop", "hello /stop", "", "   ", "/继续"]:
    check(f"不识别 {text!r}（避免把正常消息误判成指令）", scheduler.is_stop_command(text) is False)

# ══════════════════════════════════════════════════════════════
print("\n[2] 回执分工")
# ══════════════════════════════════════════════════════════════
# 2a) 打断成功：不自己发言，等被打断任务的暂停事件给回执
orch = FakeOrchestrator(ok=True, stopped=True)
reset(orch)
res = scheduler._handle_stop_command(make_task())
check("打断成功：status=stopping", res.status == "stopping", res.status)
check("打断成功：ok=True（gateway 不报错）", res.ok is True)
check("打断成功：不返回 task_id（前端不会显示「任务已创建」）", res.task_id == "", repr(res.task_id))
check("打断成功：自己不发言（否则一次 /stop 冒两条消息）", BUS.events == [], str(BUS.events))
check("打断成功：user_id 传给了 orchestrator", bool(orch.calls) and orch.calls[0]["user_id"] == "u1", str(orch.calls))

# 2b) 已挂起：标记成插话恢复 + 明确告知无需再停
orch = FakeOrchestrator(ok=True, stopped=False)
reset(orch)
suspended = make_task(user_id="u1", task_id="task-running", content="查 A 和 B")
with scheduler.SUSPENDED_LOCK:
    scheduler.SUSPENDED_TASKS["u1"] = (suspended, time.time())
res = scheduler._handle_stop_command(make_task(task_id="task-stop-2", client_message_id="msg-2"))
check("已挂起：status=already_paused", res.status == "already_paused", res.status)
check("已挂起：给挂起任务打上插话恢复标记", suspended.metadata.get("resume_mode") == "interrupt",
      str(suspended.metadata))
check("已挂起：发一条说明", len(BUS.events) == 1, str(len(BUS.events)))
check("已挂起：说明提到进度已保留", "进度已保留" in BUS.last_text(), BUS.last_text())
check("已挂起：事件类型为 assistant_message（会进对话历史）",
      BUS.events[-1].get("type") == "assistant_message")
check("已挂起：事件对用户可见（离线/别的渠道才收得到）",
      BUS.events[-1].get("metadata", {}).get("visible_to_user") == "true")

# 2c) 真的空闲 + 队列里有消息：如实报告条数，且不动队列
orch = FakeOrchestrator(ok=True, stopped=False)
reset(orch)
q = queue.Queue(maxsize=10)
q.put(make_task(task_id="t-q1", content="消息一"))
q.put(make_task(task_id="t-q2", content="消息二"))
with scheduler.USER_LOCK:
    scheduler.USER_QUEUES["u1"] = q
res = scheduler._handle_stop_command(make_task(task_id="task-stop-3", client_message_id="msg-3"))
check("空闲：status=idle", res.status == "idle", res.status)
check("空闲：说明点出没有正在执行的任务", "没有正在执行的任务" in BUS.last_text(), BUS.last_text())
check("空闲：说明点出队列条数且声明未取消", "2" in BUS.last_text() and "未取消" in BUS.last_text(), BUS.last_text())
check("空闲：队列一条都没动（用户要的是「别停我排的队」）", q.qsize() == 2, str(q.qsize()))

# 2d) RPC 失败：必须如实说失败，不能假装「已停止」也不能说「没任务」
orch = FakeOrchestrator(ok=False, stopped=False, message="connection refused")
reset(orch)
res = scheduler._handle_stop_command(make_task(task_id="task-stop-4", client_message_id="msg-4"))
check("失败：status=stop_failed", res.status == "stop_failed", res.status)
check("失败：说明里带上真实原因", "connection refused" in BUS.last_text(), BUS.last_text())
check("失败：不谎报「没有正在执行的任务」", "没有正在执行的任务" not in BUS.last_text(), BUS.last_text())

# ══════════════════════════════════════════════════════════════
print("\n[3] 在 CreateTask 入口拦截")
# ══════════════════════════════════════════════════════════════
orch = FakeOrchestrator(ok=True, stopped=True)
reset(orch)
res = scheduler.submit_task(make_task(content="/stop", client_message_id="msg-9"))
check("拦截：返回 stopping（不是 queued）", res.status == "stopping", res.status)
user_q = scheduler.USER_QUEUES.get("u1")
check("拦截：没有进入队列（否则会排在运行中任务后面，永远抢不到）",
      user_q is None or user_q.empty(), f"sizes={[x.qsize() for x in scheduler.USER_QUEUES.values()]}")
check("拦截：没有登记幂等索引（/stop 不该被去重，用户可以再停一次）",
      scheduler.IDEMPOTENCY_INDEX == {}, str(scheduler.IDEMPOTENCY_INDEX))
check("拦截：确实走到了 orchestrator", bool(orch.calls), str(orch.calls))

# 对照组：普通消息不受影响 —— 证明拦截是有针对性的，不是把入口改坏了
orch = FakeOrchestrator(ok=True, stopped=True)
reset(orch)
res = scheduler.submit_task(make_task(content="帮我查 A", task_id="task-n1", client_message_id="msg-n1"))
check("对照：普通消息照常入队", scheduler.USER_QUEUES["u1"].qsize() == 1,
      str(scheduler.USER_QUEUES["u1"].qsize()))
check("对照：普通消息照常登记幂等索引", len(scheduler.IDEMPOTENCY_INDEX) == 1,
      str(scheduler.IDEMPOTENCY_INDEX))
check("对照：普通消息不会去调 orchestrator 的停止接口", orch.calls == [], str(orch.calls))
check("对照：普通消息发出 task_queued 事件",
      any(e.get("type") == "task_queued" for e in BUS.events), str([e.get("type") for e in BUS.events]))

# ══════════════════════════════════════════════════════════════
print("\n[4] 队列竞态消化：暂停落地前已排上的消息 → 升级为插话")
# ══════════════════════════════════════════════════════════════
orch = FakeOrchestrator(ok=True, stopped=True)
reset(orch)

suspended = make_task(user_id="u5", task_id="task-susp", content="查 A")
with scheduler.SUSPENDED_LOCK:
    scheduler.SUSPENDED_TASKS["u5"] = (suspended, time.time())

q = queue.Queue(maxsize=10)
queued = make_task(user_id="u5", task_id="task-queued", content="那改成 B")
q.put(queued)
with scheduler.USER_LOCK:
    scheduler.USER_QUEUES["u5"] = q

recorded: list = []
original_resume = scheduler._resume_suspended
scheduler._resume_suspended = lambda s, r: recorded.append((s, r))
try:
    scheduler._drain_queued_into_interject(suspended)
finally:
    scheduler._resume_suspended = original_resume

check("消化：把队首消息交给挂起任务作为插话", len(recorded) == 1 and recorded[0][1] is queued,
      str(recorded))
check("消化：挂起任务对象正确", bool(recorded) and recorded[0][0] is suspended)
check("消化：队首已出队（不会被当成独立新任务又跑一遍）", q.empty(), str(q.qsize()))

# 队列为空时不动作（此时用户下一条消息会走正常挂起恢复路径）
reset(orch)
suspended2 = make_task(user_id="u6", task_id="task-susp2", content="查 A")
with scheduler.SUSPENDED_LOCK:
    scheduler.SUSPENDED_TASKS["u6"] = (suspended2, time.time())
recorded2: list = []
scheduler._resume_suspended = lambda s, r: recorded2.append((s, r))
try:
    scheduler._drain_queued_into_interject(suspended2)
finally:
    scheduler._resume_suspended = original_resume
check("消化：队列为空时什么都不做", recorded2 == [], str(recorded2))

# ══════════════════════════════════════════════════════════════
print("\n" + "=" * 60)
total = len(PASSED) + len(FAILED)
print(f"/stop 调度器侧冒烟测试：{len(PASSED)}/{total} PASS")
if FAILED:
    print("失败项：")
    for name in FAILED:
        print(f"  - {name}")
    sys.exit(1)
print("全部通过")

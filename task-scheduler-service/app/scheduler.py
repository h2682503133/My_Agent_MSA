# scheduler.py 完整原版功能 + 只接收 task + 无重试（只执行一次）
# 微服务化版本：保留原调度核心；scheduler 只处理轻量 ScheduledTask，
# 不再持有 Agent Runtime / 原 Task 的压栈弹栈状态。
import queue
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass

from app import config
from app.event_bus import event_bus, task_event
from app.logger import gateway_log
from app.orchestrator_client import OrchestratorClient
from app.scheduled_task import ScheduledTask

# ======================
# 全局配置
# ======================
queue_clients = set()
processed = 0
MAX_RETRY = 0  # 🔥 改成 0 = 不重试
MAX_TASK_TIME = config.MAX_TASK_TIME

USER_QUEUES: OrderedDict[str, queue.Queue] = OrderedDict()
BATCH_SIZE = config.BATCH_SIZE
BATCH_SLOTS = [None] * BATCH_SIZE
BUSY_USERS: set[str] = set()

USER_LOCK = threading.Lock()
BUSY_LOCK = threading.Lock()
SLOTS_LOCK = threading.Lock()
TASK_AVAILABLE = threading.Condition()

# client_message_id 幂等索引：gateway / channel-gateway 重试 CreateTask 时不会重复入队。
IDEMPOTENCY_LOCK = threading.Lock()
IDEMPOTENCY_INDEX: dict[tuple[str, str, str], str] = {}

_ORCHESTRATOR_CLIENT: OrchestratorClient | None = None
_SCHEDULER_STARTED = False
_SCHEDULER_STARTED_LOCK = threading.Lock()

TERMINAL_EVENT_TYPES = {
    "task_completed",
    "task_failed",
    "task_timeout",
    "task_cancelled",
    "task_finished_with_error",
}
FAIL_EVENT_TYPES = {
    "task_failed",
    "task_timeout",
    "task_error",
    "task_cancelled",
    "task_finished_with_error",
}


@dataclass
class SubmitResult:
    ok: bool
    task_id: str
    status: str
    waiting: int
    duplicate: bool = False
    error: str = ""


# ======================
# SSE 队列刷新（保留原意：通知有队列变化）
# ======================
def notify_queue_update():
    for client in list(queue_clients):
        try:
            client.put(True)
        except Exception:
            queue_clients.remove(client)


def waiting_count() -> int:
    with USER_LOCK:
        return sum(q.qsize() for q in USER_QUEUES.values())


def _has_pending_task() -> bool:
    with USER_LOCK:
        return any(not q.empty() for q in USER_QUEUES.values())


def _wake_scheduler() -> None:
    with TASK_AVAILABLE:
        TASK_AVAILABLE.notify()


# ======================
# 槽位调度器（核心逻辑 · 继承原用户队列 + 槽位规则）
# ======================
def slot_scheduler():
    global processed
    while True:
        if not _has_pending_task():
            with TASK_AVAILABLE:
                TASK_AVAILABLE.wait(timeout=0.5)
            continue

        with USER_LOCK:
            user_list = list(USER_QUEUES.keys())

        dispatched = False
        for user_id in user_list:
            if processed >= BATCH_SIZE:
                break

            try:
                with USER_LOCK:
                    user_q = USER_QUEUES.get(user_id)
                if user_q is None or user_q.empty():
                    continue

                with BUSY_LOCK:
                    if user_id in BUSY_USERS:
                        continue

                with SLOTS_LOCK:
                    if None not in BATCH_SLOTS:
                        break
                    slot_idx = BATCH_SLOTS.index(None)
                    BATCH_SLOTS[slot_idx] = user_id

                # ------------------------------
                # 取出队首任务：微服务化后使用 get_nowait() 正式 claim 任务
                # ------------------------------
                try:
                    task: ScheduledTask = user_q.get_nowait()
                except queue.Empty:
                    with SLOTS_LOCK:
                        BATCH_SLOTS[slot_idx] = None
                    continue

                # ------------------------------
                # 🔥 禁用重试：永远只执行一次
                # ------------------------------
                task.retry_count = 0
                task.status = "running"
                task.slot_index = slot_idx

                with BUSY_LOCK:
                    BUSY_USERS.add(user_id)

                processed += 1
                dispatched = True

                threading.Thread(
                    target=run_task,
                    args=(task,),
                    daemon=True,
                ).start()

            except Exception as e:
                gateway_log(f"slot_scheduler error: {e}")
                continue

        if not dispatched:
            time.sleep(0.1)


# ======================
# 槽执行器 · 只执行一次（无重试）
# ======================
def run_task(task: ScheduledTask):
    global processed
    user_id = task.user_id

    # send_message 定时任务：直接推送消息，不走 orchestrator
    if task.metadata.get("timer_type") == "send_message":
        gateway_log(f"{task.slot_index}号槽正处理{user_id}的定时消息，直接推送")
        event_bus.publish(task_event(task, "task_started", waiting=waiting_count()))
        event_bus.publish(task_event(
            task, "assistant_message",
            text=task.content,
            waiting=waiting_count(),
            metadata={"visible_to_user": "true", "final": "true"},
        ))
        event_bus.publish(task_event(task, "task_completed", waiting=waiting_count()))
        with SLOTS_LOCK:
            if 0 <= task.slot_index < len(BATCH_SLOTS):
                BATCH_SLOTS[task.slot_index] = None
        with BUSY_LOCK:
            BUSY_USERS.discard(user_id)
        processed -= 1
        _wake_scheduler()
        return

    success = True
    saw_terminal_event = False
    client = _ORCHESTRATOR_CLIENT or OrchestratorClient()

    gateway_log(f"{task.slot_index}号槽正处理{user_id}的请求，仅执行一次")
    event_bus.publish(task_event(task, "task_started", waiting=waiting_count()))

    try:
        task.status = "running"
        for event in client.execute_task(task):
            event_type = event.get("type", "")
            if event_type in FAIL_EVENT_TYPES or event.get("error"):
                success = False
            if event_type in TERMINAL_EVENT_TYPES:
                saw_terminal_event = True
            event_bus.publish(event)

    except Exception as e:
        success = False
        event_bus.publish(task_event(task, "task_failed", error=str(e), waiting=waiting_count()))

    finally:
        with SLOTS_LOCK:
            if 0 <= task.slot_index < len(BATCH_SLOTS):
                BATCH_SLOTS[task.slot_index] = None
        with BUSY_LOCK:
            BUSY_USERS.discard(user_id)
        processed -= 1

    task.status = "completed" if success else "failed"

    # orchestrator 如果没有发 terminal event，scheduler 负责补一个终态事件。
    if not saw_terminal_event:
        event_bus.publish(task_event(
            task,
            "task_completed" if success else "task_failed",
            waiting=waiting_count(),
            error="" if success else "任务未正常完成",
        ))

    _wake_scheduler()


# ======================
# 通用提交接口（gateway/channel-gateway 调用）
# ======================
def submit_task(task: ScheduledTask) -> SubmitResult:
    idempotency_key = task.idempotency_key
    if idempotency_key is not None:
        with IDEMPOTENCY_LOCK:
            existing_task_id = IDEMPOTENCY_INDEX.get(idempotency_key)
            if existing_task_id:
                return SubmitResult(
                    ok=True,
                    task_id=existing_task_id,
                    status="duplicate",
                    waiting=waiting_count(),
                    duplicate=True,
                )
            IDEMPOTENCY_INDEX[idempotency_key] = task.task_id

    user_id = task.user_id
    with USER_LOCK:
        if user_id not in USER_QUEUES:
            USER_QUEUES[user_id] = queue.Queue(maxsize=config.USER_QUEUE_SIZE)
        q = USER_QUEUES[user_id]
        try:
            q.put_nowait(task)
        except queue.Full:
            return SubmitResult(
                ok=False,
                task_id=task.task_id,
                status="queue_full",
                waiting=waiting_count(),
                error=f"user queue full: {user_id}",
            )

    current_waiting = waiting_count()
    task.waiting = current_waiting
    notify_queue_update()
    event_bus.publish(task_event(task, "task_queued", waiting=current_waiting))
    _wake_scheduler()

    return SubmitResult(
        ok=True,
        task_id=task.task_id,
        status=task.status,
        waiting=current_waiting,
    )


def start_scheduler(orchestrator_client: OrchestratorClient | None = None):
    global _ORCHESTRATOR_CLIENT, _SCHEDULER_STARTED
    _ORCHESTRATOR_CLIENT = orchestrator_client or OrchestratorClient()

    with _SCHEDULER_STARTED_LOCK:
        if _SCHEDULER_STARTED:
            return
        threading.Thread(target=slot_scheduler, daemon=True).start()
        _SCHEDULER_STARTED = True

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
from app.scheduled_task import DeliveryTarget, ScheduledTask

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

# 询问挂起任务：user_id -> (ScheduledTask, suspend_at)
# 挂起时用户保留在 BUSY_USERS（阻滞该用户后续任务），但任务不占用并发槽位；
# 并发上限按「BATCH_SIZE + 挂起用户数」计算，挂起用户不占用真实并发额度。
SUSPENDED_TASKS: dict[str, tuple[ScheduledTask, float]] = {}

USER_LOCK = threading.Lock()
BUSY_LOCK = threading.Lock()
SLOTS_LOCK = threading.Lock()
SUSPENDED_LOCK = threading.Lock()
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


def _suspended_count() -> int:
    with SUSPENDED_LOCK:
        return len(SUSPENDED_TASKS)


def _concurrency_limit() -> int:
    """并发上限 = 设定值 + 挂起用户数（挂起任务不占用真实并发额度）。"""
    return BATCH_SIZE + _suspended_count()


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
            if processed >= _concurrency_limit():
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
        agent_prefix = f"{task.agent_id}:" if task.agent_id else ""
        event_bus.publish(task_event(
            task, "assistant_message",
            text=agent_prefix + task.content,
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

    client = _ORCHESTRATOR_CLIENT or OrchestratorClient()

    gateway_log(f"{task.slot_index}号槽正处理{user_id}的请求，仅执行一次")
    event_bus.publish(task_event(task, "task_started", waiting=waiting_count()))

    task.status = "running"
    _run_execution(task, lambda: client.execute_task(task))


def _run_execution(task: ScheduledTask, stream_factory):
    """消费 orchestrator 事件流并发布；处理询问挂起与终态补发。"""
    global processed
    user_id = task.user_id

    success = True
    saw_terminal_event = False
    suspended = False

    try:
        for event in stream_factory():
            event_type = event.get("type", "")
            if event_type == "task_waiting_user":
                # 询问挂起：登记挂起任务并释放物理槽位。
                # 用户保留在 BUSY_USERS（阻滞该用户后续任务），processed 也保留计数，
                # 但通过并发上限 +挂起数使其不占用真实并发额度。
                suspended = True
                task.status = "suspended"
                with SUSPENDED_LOCK:
                    SUSPENDED_TASKS[user_id] = (task, time.time())
                with SLOTS_LOCK:
                    if 0 <= task.slot_index < len(BATCH_SLOTS):
                        BATCH_SLOTS[task.slot_index] = None
                event_bus.publish(event)
                continue
            if event_type in FAIL_EVENT_TYPES or event.get("error"):
                success = False
            if event_type in TERMINAL_EVENT_TYPES:
                saw_terminal_event = True
            # 在发送给用户前添加智能体id前缀（不存储到viking）
            if event_type == "assistant_message" and event.get("metadata", {}).get("visible_to_user") == "true":
                agent_id = task.agent_id or event.get("metadata", {}).get("agent_id", "")
                if agent_id:
                    event["text"] = agent_id + ":" + event.get("text", "")
            event_bus.publish(event)

    except Exception as e:
        success = False
        event_bus.publish(task_event(task, "task_failed", error=str(e), waiting=waiting_count()))

    finally:
        if suspended:
            # 挂起任务不结束：不释放 BUSY_USERS / processed，不补终态事件。
            # 等用户回复或超时后由 _run_resume 恢复。
            return
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


def _claim_suspended(user_id: str, expected_task_id: str) -> ScheduledTask | None:
    """原子占位：从挂起注册表取走任务，保证同一挂起任务只被恢复一次。"""
    with SUSPENDED_LOCK:
        current = SUSPENDED_TASKS.get(user_id)
        if current is None or current[0].task_id != expected_task_id:
            return None
        del SUSPENDED_TASKS[user_id]
    return current[0]


def _resume_suspended(suspended: ScheduledTask, reply_task: ScheduledTask) -> SubmitResult:
    """用户回复（或超时系统回复）恢复挂起任务：不排队，后台线程等待空闲槽位后执行。"""
    claimed = _claim_suspended(suspended.user_id, suspended.task_id)
    if claimed is None:
        return SubmitResult(
            ok=False,
            task_id=suspended.task_id,
            status="resume_conflict",
            waiting=waiting_count(),
            error="挂起任务已被恢复",
        )

    threading.Thread(target=_run_resume, args=(claimed, reply_task), daemon=True).start()
    notify_queue_update()
    _wake_scheduler()
    return SubmitResult(
        ok=True,
        task_id=suspended.task_id,
        status="resumed",
        waiting=waiting_count(),
    )


def _run_resume(suspended: ScheduledTask, reply_task: ScheduledTask) -> None:
    """等待空闲物理槽位，然后调用 orchestrator ResumeTask 恢复挂起任务。"""
    user_id = suspended.user_id

    # 挂起期间任务已释放物理槽位，恢复前等待空闲槽位
    slot_idx = -1
    while True:
        with SLOTS_LOCK:
            if None in BATCH_SLOTS:
                slot_idx = BATCH_SLOTS.index(None)
                BATCH_SLOTS[slot_idx] = user_id
                break
        time.sleep(0.1)

    resume_task = ScheduledTask(
        task_id=suspended.task_id,
        user_id=user_id,
        session_id=suspended.session_id,
        channel=suspended.channel,
        content=reply_task.content,
        # 最终回复仍引用最初那条任务消息（原始 client_message_id）
        client_message_id=suspended.client_message_id,
        delivery_target=DeliveryTarget(
            channel=suspended.delivery_target.channel,
            user_id=suspended.delivery_target.user_id,
            conversation_id=suspended.delivery_target.conversation_id,
            reply_to=suspended.delivery_target.reply_to or suspended.client_message_id,
        ),
        metadata=dict(suspended.metadata),
        agent_id=suspended.agent_id,
        images=list(reply_task.images or []),
    )
    resume_task.status = "running"
    resume_task.slot_index = slot_idx
    resume_task.retry_count = 0

    client = _ORCHESTRATOR_CLIENT or OrchestratorClient()
    gateway_log(f"{slot_idx}号槽恢复{user_id}的挂起任务 {suspended.task_id}")
    event_bus.publish(task_event(
        resume_task,
        "task_resumed",
        waiting=waiting_count(),
        metadata={"task_id": suspended.task_id},
    ))

    # 超时系统回复：不发布到用户，只作为模型输入
    if reply_task.task_id.startswith("timeout-"):
        gateway_log(f"[suspend-ttl] {user_id} 的任务 {suspended.task_id} 超时，系统替用户回复并恢复")

    _run_execution(resume_task, lambda: client.resume_task(resume_task, reply_task.content))


def _build_timeout_reply(suspended: ScheduledTask) -> ScheduledTask:
    """构造超时系统回复：提示模型用户未回复，让模型自行判断，不删除挂起任务。"""
    content = "【系统提示】用户未在规定时间内回复，请根据情况自行判断，不要继续等待用户回复。"
    return ScheduledTask(
        task_id=f"timeout-{suspended.task_id}",
        user_id=suspended.user_id,
        session_id=suspended.session_id,
        channel=suspended.channel,
        content=content,
        client_message_id="",
        delivery_target=suspended.delivery_target,
        metadata=dict(suspended.metadata),
        agent_id=suspended.agent_id,
    )


def _suspend_ttl_sweeper() -> None:
    """扫描超时未回复的挂起任务：系统替用户回复并恢复，让模型自行判断。"""
    while True:
        time.sleep(config.SUSPEND_SWEEP_INTERVAL)
        now = time.time()
        expired: list[tuple[ScheduledTask, ScheduledTask]] = []
        with SUSPENDED_LOCK:
            for user_id, (suspended, suspend_at) in list(SUSPENDED_TASKS.items()):
                if now - suspend_at >= config.SUSPEND_TTL_SECONDS:
                    expired.append((suspended, _build_timeout_reply(suspended)))
        for suspended, timeout_task in expired:
            _resume_suspended(suspended, timeout_task)


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

    # 挂起恢复路由：该用户存在挂起任务时，本条消息视为对挂起任务的回复，不排队。
    with SUSPENDED_LOCK:
        suspended_entry = SUSPENDED_TASKS.get(user_id)
    if suspended_entry is not None:
        return _resume_suspended(suspended_entry[0], task)

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
        threading.Thread(target=_suspend_ttl_sweeper, daemon=True).start()
        _SCHEDULER_STARTED = True

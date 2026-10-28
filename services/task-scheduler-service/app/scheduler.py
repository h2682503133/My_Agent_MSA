# scheduler.py 完整原版功能 + 只接收 task + 无重试（只执行一次）
# 微服务化版本：保留原调度核心；scheduler 只处理轻量 ScheduledTask，
# 不再持有 Agent Runtime / 原 Task 的压栈弹栈状态。
import queue
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass

from app import config
from app.event_bus import event_bus, task_event
from app.logger import gateway_log
from app.orchestrator_client import OrchestratorClient
from app import commands
from app import task_merge
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
# /stop 打断（barge-in）
# ======================
# 语义是**暂停**而不是取消：orchestrator 让正在跑的任务在下一个检查点停下，
# 现场（上下文栈 / tool_log / 记忆）全部保留；用户下一条消息作为【用户插话】
# 回到同一个任务继续跑。
#
# 必须在 CreateTask 入口就拦下：/stop 若按普通任务入队，会排在正在执行的任务
# 之后，永远没机会抢占 —— 而「用户正需要它生效」恰恰就是有任务在跑的时候。
# 全角斜杠一并接受（中文输入法下很容易打成 ／stop）。
# ─── 短窗口合并状态 ───────────────────────────────────────
_QUEUED_REFS: dict[str, ScheduledTask] = {}
_QUEUED_REFS_LOCK = threading.Lock()


def _merge_channel_allowed(channel: str) -> bool:
    allowed = config.SCHEDULER_MERGE_HOLD_CHANNELS
    if not allowed:
        return False
    if "*" in allowed or "all" in allowed:
        return True
    return (channel or "").strip().lower() in allowed


def _enqueue_task(task: ScheduledTask) -> SubmitResult:
    """把任务放进该用户的队列并发事件（滞留释放与普通提交共用）。"""
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
        with _QUEUED_REFS_LOCK:
            _QUEUED_REFS[user_id] = task

    current_waiting = waiting_count()
    task.waiting = current_waiting
    notify_queue_update()
    event_bus.publish(task_event(task, "task_queued", waiting=current_waiting))
    _wake_scheduler()
    return SubmitResult(ok=True, task_id=task.task_id, status=task.status, waiting=current_waiting)


def _release_held_task(user_id: str, task: ScheduledTask) -> None:
    gateway_log(f"[merge] {user_id} 滞留结束，入队 {task.task_id}")
    _enqueue_task(task)


_HOLD_QUEUE = task_merge.HoldQueue(_release_held_task, logger=gateway_log)


def _merge_or_hold(task: ScheduledTask, is_push_only: bool) -> SubmitResult | None:
    """合并/滞留判定。返回 None 表示按原逻辑排队。"""
    if not config.SCHEDULER_MERGE_QUEUED or is_push_only:
        return None
    if str(task.metadata.get("source") or "") == "timer_task":
        return None
    if not _merge_channel_allowed(task.channel):
        return None

    user_id = task.user_id
    with BUSY_LOCK:
        busy = user_id in BUSY_USERS
    if busy:
        return None

    max_parts = int(config.SCHEDULER_MERGE_MAX_PARTS or 3)

    # 已有排队未开跑的任务 → 直接并入（零延迟）
    with _QUEUED_REFS_LOCK:
        target = _QUEUED_REFS.get(user_id)
    if target is not None:
        with USER_LOCK:
            still_queued = target.task_id in {
                t.task_id for t in list(USER_QUEUES.get(user_id).queue)
            } if USER_QUEUES.get(user_id) is not None else False
        if still_queued and task_merge.merge_task(target, task, max_parts):
            gateway_log(
                f"[merge] {user_id} 并入排队任务 {target.task_id} <- {task.task_id} "
                f"(parts={target.metadata.get('_merge_parts')})"
            )
            return SubmitResult(
                ok=True,
                task_id=target.task_id,
                status="merged",
                waiting=waiting_count(),
            )

    hold_ms, hold_max_ms = task_merge.hold_window(
        task,
        int(config.SCHEDULER_MERGE_HOLD_MS or 1200),
        int(config.SCHEDULER_MERGE_HOLD_MAX_MS or 2500),
        int(getattr(config, "SCHEDULER_MERGE_HOLD_MS_IMAGE", 3000) or 3000),
        int(getattr(config, "SCHEDULER_MERGE_HOLD_MAX_MS_IMAGE", 6000) or 6000),
    )
    if task_merge.has_images(task):
        gateway_log(f"[merge] {user_id} 带图片，滞留窗口用 {hold_ms}ms")
    verdict = _HOLD_QUEUE.hold(user_id, task, hold_ms, hold_max_ms, max_parts)
    if verdict == "merged":
        held = _HOLD_QUEUE._items.get(user_id)  # noqa: SLF001 - 仅取 id 用于回执
        task_id = held["task"].task_id if held else task.task_id
        return SubmitResult(ok=True, task_id=task_id, status="merged", waiting=waiting_count())
    return SubmitResult(ok=True, task_id=task.task_id, status="holding", waiting=waiting_count())


def is_stop_command(content: str) -> bool:
    """内容是否是 /stop 打断指令（保留此名给既有调用点；解析见 app/commands.py）。"""
    command = commands.parse(content)
    return bool(command and command.is_stop)


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
                with _QUEUED_REFS_LOCK:
                    _QUEUED_REFS.pop(user_id, None)
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
    interrupted = False

    try:
        for event in stream_factory():
            event_type = event.get("type", "")
            # 去重：task_started 由 scheduler 统一发布（run_task L213 / send_message 定时任务 L192），
            # orchestrator 流回传的 task_started 会重复触发前端「任务开始」提示，直接丢弃。
            if event_type == "task_started":
                continue
            if event_type == "task_waiting_user":
                # 询问挂起：登记挂起任务并释放物理槽位。
                # 用户保留在 BUSY_USERS（阻滞该用户后续任务），processed 也保留计数，
                # 但通过并发上限 +挂起数使其不占用真实并发额度。
                suspended = True
                task.status = "suspended"
                # interrupt=true = /stop 打断式暂停（保留现场等插话），
                # 与「询问」挂起区分开（见 _drain_queued_into_interject）。
                interrupted = str((event.get("metadata") or {}).get("interrupt") or "").lower() == "true"
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
            if interrupted:
                _drain_queued_into_interject(task)
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


def _drain_queued_into_interject(suspended: ScheduledTask) -> None:
    """打断暂停后，把队列里已经排上的消息直接升级成插话接上。

    为什么必须有这一步：/stop 的暂停发生在「下一个检查点」，用户点完停止通常
    会马上再发一条。那条消息若赶在暂停落地之前到达，就已经落在队列里了；而
    队列要等挂起任务恢复才会动 —— 两边互等，用户看到的就是消息被吞了，
    直到他再发一条、或挂起超时（默认 1 小时）才恢复。

    这里取队首一条交给 _resume_suspended，把它变成【用户插话】。队列里其余的
    仍然保留（用户要的是「别停我排的队」，不是「清空我的消息」）。
    """
    user_id = suspended.user_id
    with USER_LOCK:
        user_q = USER_QUEUES.get(user_id)
    if user_q is None or user_q.empty():
        return
    try:
        queued = user_q.get_nowait()
    except queue.Empty:
        return

    queued.waiting = waiting_count()
    gateway_log(
        f"[interject] {user_id} 打断暂停后队列中已有消息 {queued.task_id}，"
        f"直接作为插话接上（不排队）"
    )
    _resume_suspended(suspended, queued)


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


def _build_offline_reply(suspended: ScheduledTask) -> ScheduledTask:
    """构造「用户离线」系统代答：用户当前无法回复（消息只能靠推送/转投送达），
    让模型自行判断与执行、不要再等待用户回复。

    与超时代答的区别只在提示语和 task_id 前缀（便于日志区分触发来源）。
    """
    content = "【系统提示】用户当前无法回复，请自行进行判断与执行，不要继续等待用户回复。"
    return ScheduledTask(
        task_id=f"offline-{suspended.task_id}",
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
# /stop 打断处理
# ======================
def _publish_stop_notice(task: ScheduledTask, text: str) -> None:
    """把 /stop 的回执作为用户可见消息投递。

    走正常事件链路（ack / 跨渠道转投 / 离线缓冲）—— 用户可能是在 QQ 上按的
    停止，也可能 web 页面当时并不在线。
    """
    event_bus.publish(task_event(
        task,
        "assistant_message",
        text=text,
        waiting=waiting_count(),
        metadata={
            "visible_to_user": "true",
            "final": "true",
            # 带上 agent_id：gateway 据此把这条回执记到当前对话（默认会记到 main）
            "agent_id": getattr(task, "agent_id", "") or "main",
        },
    ))


def _handle_state_command(task: ScheduledTask, command: "commands.Command") -> SubmitResult:
    """处理 /agent、/mode：请求 orchestrator 改会话状态，并把回执发给用户。

    状态只存在 orchestrator 内存里（`AgentRuntime.default_agent` / `default_mode`），
    scheduler 侧不做任何本地存储，纯转发。
    """
    kind, arg = command.name, command.arg
    client = _ORCHESTRATOR_CLIENT or OrchestratorClient()
    result = client.set_session_state(
        user_id=task.user_id,
        session_id=task.session_id,
        agent_id=arg if kind == "agent" else "",
        mode=arg if kind == "mode" else "",
    )
    waiting = waiting_count()

    if result.ok:
        gateway_log(f"[state] {task.user_id} /{kind} {arg or '(查看)'} ok")
        _publish_stop_notice(task, result.message or "已更新会话状态")
        return SubmitResult(ok=True, task_id="", status="state_command", waiting=waiting)

    detail = result.error or result.message or "未知错误"
    gateway_log(f"[state] {task.user_id} /{kind} {arg or '(查看)'} 失败：{detail}")
    _publish_stop_notice(task, f"【命令失败】{detail}")
    return SubmitResult(ok=True, task_id="", status="state_command_failed", waiting=waiting)


def _handle_command(task: ScheduledTask, command: "commands.Command") -> SubmitResult:
    """所有斜杠命令的唯一分发点（/stop、/agent、/mode）。"""
    if command.is_stop:
        return _handle_stop_command(task)
    return _handle_state_command(task, command)


def _handle_stop_command(task: ScheduledTask) -> SubmitResult:
    """处理 /stop：请求 orchestrator 暂停该用户运行中的任务（保留现场，等插话）。

    回执的分工：打断**成功**时不在这里发言 —— 被打断的任务会发出
    task_waiting_user（interrupt=true）事件，由它给出「已停止，进度已保留」，
    一次 /stop 只冒一条消息。只有「没东西可停」和「请求失败」才在这里补说明。
    """
    user_id = task.user_id
    client = _ORCHESTRATOR_CLIENT or OrchestratorClient()
    result = client.request_stop(user_id=user_id, reason="user /stop")
    waiting = waiting_count()

    if not result.ok:
        gateway_log(f"[stop] {user_id} 停止请求失败：{result.message}")
        _publish_stop_notice(task, f"停止请求发送失败：{result.message}")
        return SubmitResult(ok=True, task_id="", status="stop_failed", waiting=waiting)

    if result.stopped:
        gateway_log(f"[stop] {user_id} 打断请求已发出，任务将在下一个检查点暂停")
        return SubmitResult(ok=True, task_id="", status="stopping", waiting=waiting)

    # 没有运行中的任务。两种可能，回执要分清楚：
    #   1) 已经处于暂停（询问 / 上一次打断）：本来就等着用户说话，无需再停，
    #      但要把它标成「插话恢复」——用户下一条消息就不是在回答那个问题，
    #      而是接着往下说的插话。
    #   2) 真的空闲。
    with SUSPENDED_LOCK:
        suspended_entry = SUSPENDED_TASKS.get(user_id)
    if suspended_entry is not None:
        suspended_entry[0].metadata["resume_mode"] = "interrupt"
        gateway_log(f"[stop] {user_id} 任务已挂起，标记为插话恢复模式")
        _publish_stop_notice(
            task, "任务已处于暂停状态，进度已保留。直接发消息即可接着继续。"
        )
        return SubmitResult(ok=True, task_id="", status="already_paused", waiting=waiting)

    with USER_LOCK:
        user_q = USER_QUEUES.get(user_id)
    pending = user_q.qsize() if user_q is not None else 0
    notice = "当前没有正在执行的任务。"
    if pending:
        notice += f"（队列中还有 {pending} 条消息，未取消）"
    gateway_log(f"[stop] {user_id} 无运行中任务（队列 {pending} 条）")
    _publish_stop_notice(task, notice)
    return SubmitResult(ok=True, task_id="", status="idle", waiting=waiting)


# ======================
# 通用提交接口（gateway/channel-gateway 调用）
# ======================
def submit_task(task: ScheduledTask) -> SubmitResult:
    # /stop 必须在幂等登记之前拦下：它不会真的成为一个任务，
    # 也不该被 client_message_id 去重（用户停一次不够时应能再停一次）。
    # 所有斜杠命令（/stop、/agent、/mode）统一在入队前拦下：控制命令不是任务内容，
    # 也不该被幂等键去重；解析与分发给 app/commands.py + _handle_command。
    command = commands.parse(task.content)
    if command is not None:
        return _handle_command(task, command)

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

    # 推送类任务（定时提醒 timer_type=send_message、离线转投 offline_fallback）：
    # 其 content 是「要发给用户的消息」，不是用户输入。
    is_push_only = (
        task.metadata.get("timer_type") == "send_message"
        or task.metadata.get("offline_fallback") == "1"
    )

    # 挂起恢复路由：该用户存在挂起任务时，本条消息视为对挂起任务的回复，不排队。
    with SUSPENDED_LOCK:
        suspended_entry = SUSPENDED_TASKS.get(user_id)
    if suspended_entry is not None:
        if is_push_only:
            # 用户当前收不到交互式回复（消息只能靠推送/转投送达 → 人不在可交互渠道上）：
            # 不能把推送内容当作他的回复（否则模型再提问 → 再次推送转投 → 往复循环）。
            # 正确做法：系统代答「用户当前无法回复，请自行判断执行」，让模型自己收尾。
            # 本条推送任务不 return，继续走下面的入队流程照常推送。
            suspended_task = suspended_entry[0]
            used = int(suspended_task.metadata.get("_offline_reply_count", "0") or "0")
            if used < config.MAX_OFFLINE_REPLIES:
                suspended_task.metadata["_offline_reply_count"] = str(used + 1)
                threading.Thread(
                    target=_resume_suspended,
                    args=(suspended_task, _build_offline_reply(suspended_task)),
                    daemon=True,
                ).start()
                gateway_log(
                    f"[offline-reply] {user_id} 收到推送类任务且存在挂起任务 "
                    f"{suspended_task.task_id}，改为系统代答恢复"
                    f"（第 {used + 1}/{config.MAX_OFFLINE_REPLIES} 次）"
                )
            else:
                gateway_log(
                    f"[offline-reply] {user_id} 挂起任务 {suspended_task.task_id} "
                    f"离线代答已达上限 {config.MAX_OFFLINE_REPLIES}，不再代答（推送任务照常发送）"
                )
        else:
            return _resume_suspended(suspended_entry[0], task)

    merged_result = _merge_or_hold(task, bool(is_push_only))
    if merged_result is not None:
        return merged_result

    return _enqueue_task(task)


def start_scheduler(orchestrator_client: OrchestratorClient | None = None):
    global _ORCHESTRATOR_CLIENT, _SCHEDULER_STARTED
    _ORCHESTRATOR_CLIENT = orchestrator_client or OrchestratorClient()

    with _SCHEDULER_STARTED_LOCK:
        if _SCHEDULER_STARTED:
            return
        threading.Thread(target=slot_scheduler, daemon=True).start()
        threading.Thread(target=_suspend_ttl_sweeper, daemon=True).start()
        _SCHEDULER_STARTED = True

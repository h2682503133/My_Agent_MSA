"""
TaskRuntime 是原 core/Task/Task.py 在 orchestrator 内部的运行时版本。

注意：
- 它不跨服务传输。
- scheduler 只传 ExecuteTaskRequest。
- orchestrator 收到请求后才构造 TaskRuntime。
"""

import datetime
import threading
from dataclasses import dataclass, field
from typing import Any


@dataclass
class RuntimeUser:
    id: str
    session_id: str


@dataclass
class TaskRuntime:
    task_id: str
    user: RuntimeUser
    content: str
    channel: str
    created_at: str = ""
    metadata: dict[str, str] = field(default_factory=dict)
    agent_id: str = ""
    images: list[str] = field(default_factory=list)

    status: str = "running"
    default_agent: Any = None
    retry_count: int = 0
    slot_index: int = -1

    # 核心上下文栈：保留原 Task.py 的压栈 / 弹栈思想
    agent_context: list[dict[str, Any]] = field(default_factory=list)
    caller: Any = None
    target: Any = None

    temp_dialog_input: Any = None
    temp_dialog_output: Any = None
    temp_dialog_images: list[str] = field(default_factory=list)
    last_dialog_content: str = ""

    main_memory: list[str] = field(default_factory=list)
    task_memory: list[str] = field(default_factory=list)
    tool_log: list[str] = field(default_factory=list)
    main_log: list[str] = field(default_factory=list)
    intermediate_texts: list[str] = field(default_factory=list)

    send_images: list[str] = field(default_factory=list)
    send_text: str = ""

    # ─── 插话（barge-in）────────────────────────────────────────
    # /stop 打断**不是销毁**：任务在下一个检查点暂停，上面所有现场字段
    # （agent_context 栈 / tool_log / main_memory / temp_dialog_*）原样保留，
    # 用户下一条消息作为【用户插话】并入同一任务继续跑。
    #   interrupt_event     : 运行中收到停止请求时置位；任务在自己的检查点观察它
    #   paused_by_interrupt : 本次挂起来自 /stop（而非「询问」），恢复时走插话路径
    #   pending_interjection: 待并入下一轮的插话文本
    interrupt_event: threading.Event = field(
        default_factory=threading.Event, repr=False, compare=False
    )
    paused_by_interrupt: bool = False
    pending_interjection: str = ""

    def __post_init__(self) -> None:
        self.caller = self.user
        self.set_temp_dialog_input(
            f"请求时间:{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')},请求内容:{self.content}"
        )
        self.push_context(self.user, self.content)

    @classmethod
    def from_execute_request(cls, request) -> "TaskRuntime":
        user = RuntimeUser(id=request.user_id, session_id=request.session_id)
        return cls(
            task_id=request.task_id,
            user=user,
            content=request.content,
            channel=request.channel,
            created_at=request.created_at,
            metadata=dict(request.metadata),
            agent_id=request.agent_id or "",
            images=list(request.images or []),
        )

    def push_context(self, from_obj, input_text: str) -> None:
        self.agent_context.append({
            "from": from_obj,
            "input": input_text,
        })

    def pop_context(self) -> dict | None:
        if self.agent_context:
            return self.agent_context.pop()
        return None

    def set_temp_dialog_input(self, input_text) -> None:
        self.temp_dialog_input = input_text

    def consume_temp_dialog_input(self):
        value = self.temp_dialog_input
        self.temp_dialog_input = None
        return value

    def set_temp_dialog_output(self, input_text) -> None:
        self.temp_dialog_output = input_text

    def consume_temp_dialog_output(self):
        value = self.temp_dialog_output
        self.temp_dialog_output = None
        return value

    def set_temp_dialog_images(self, images) -> None:
        self.temp_dialog_images = list(images or [])

    def consume_temp_dialog_images(self):
        value = self.temp_dialog_images
        self.temp_dialog_images = []
        return value

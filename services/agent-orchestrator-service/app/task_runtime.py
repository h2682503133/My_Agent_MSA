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

    # ─── 结构化调用（文字 RPG）────────────────────────────────
    # 由 first_call() 从用户输入解析出来（见 app/prompt_plan.py）；普通聊天为 None。
    # context_builder 在组装期读取：块屏蔽 / 四个锚点插入；收尾时用 present 扇出写入。
    prompt_plan: Any = None

    @staticmethod
    def _dialog_input_text(content: str) -> str:
        """send() 真正喂给模型的本轮输入文本（带请求时间前缀）。"""
        return (
            f"请求时间:{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            f",请求内容:{content}"
        )

    def __post_init__(self) -> None:
        self.caller = self.user
        self.set_temp_dialog_input(self._dialog_input_text(self.content))
        self.push_context(self.user, self.content)

    def replace_input_content(self, new_content: str) -> None:
        """整体替换本轮输入内容（结构化调用摘掉 JSON 指令区后只剩 text）。

        `content` 在 __post_init__ 时就已经被复制成**三份**，只换 self.content 会漏掉
        模型路径与投递路径，指令区会继续进提示词并落盘到历史，所以三处必须一起换：

        ① `self.content`          —— main 的 user_input 段 + append_turn 落盘的历史
        ② `self.temp_dialog_input` —— send() 里 consume 后真正发给模型的那份
        ③ `agent_context` 栈底帧的 input —— 终局投递内容
           （first_call 只在栈长 == 1 时被调用，所以栈底就是用户那一帧）
        """
        self.content = new_content
        self.set_temp_dialog_input(self._dialog_input_text(new_content))
        if self.agent_context:
            self.agent_context[0]["input"] = new_content

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

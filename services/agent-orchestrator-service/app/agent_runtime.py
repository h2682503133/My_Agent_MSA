import re
import threading
from collections import OrderedDict
from typing import Callable

from app import config
from app import world_info_manager
from app.agent_config import load_agent_config
from app.context_builder import (
    ContextBuilder,
    SystemPromptParts,
    image_receive_enabled,
    load_system_settings,
    prompt_revision,
)
from app.context_client import ContextClient
from app.events import TaskEventDTO, DeliveryTarget, new_event_id
from app.logger import chat_log, debug_log
from app.model_proxy_client import ModelProxyClient
from app.response_parser import parse_model_response
from app.syntax_parser import parse_syntax
from app.tool_runtime_client import ToolRuntimeClient
from app.timer_task_client import TimerTaskClient


# /stop 打断后的用户可见提示。措辞上必须点明「进度已保留」：
# 打断是暂停而不是销毁，用户看到这句才敢直接接着说下一句。
INTERJECT_PAUSE_TEXT = "⏹ 已停止。当前进度已保留，直接发消息即可接着继续。"


class AgentRuntime:
    """
    原 core/Agent/Agent.py 的微服务化版本。

    保留的核心思想：
    - agent 实例缓存
    - first_call 默认 main
    - agent_context 压栈 / 弹栈
    - 对话 / 工具调用 / 询问 / 切换 语法解析
    - tool_log / main_log / main_memory

    拆出去的职责：
    - 上下文拼接（system prompt / messages / 世界书注入）由 app/context_builder.py 负责
    - OpenViking 由 openviking-context-service 提供
    - 模型调用由 model-proxy-service 提供
    - 工具执行由 tool-runtime-service 提供
    - 用户消息发送由 TaskEvent stream 返回 scheduler
    """

    MAX_INSTANCES = 20
    # tool-runtime 中执行 shell 的工具名（运行任意命令）
    SHELL_TOOL_NAMES = {"run-shell", "shell", "command"}
    # 写入 /mnt（Windows 宿主机目录）的工具：需过 dashboard「杂项」写白名单
    MNT_WRITE_TOOLS = {"file-copy", "file-move", "windows-file-copy", "windows-file-move"}
    _agent_instances: OrderedDict[str, "AgentRuntime"] = OrderedDict()
    default_agent: dict[str, str] = {}

    # 挂起任务注册表：task_id -> TaskRuntime
    # 只存在 orchestrator 进程内存里，供 ResumeTask 恢复时取回。
    # 挂起任务不删除：超过 1 小时由 scheduler 用系统消息替用户回复来恢复。
    PENDING_TASKS: dict[str, "TaskRuntime"] = {}
    PENDING_LOCK = threading.Lock()

    # 运行中任务注册表：user_id -> TaskRuntime（约定一用户同时只有一个任务在跑）。
    # 供 StopTask 从另一个 gRPC 线程定位到「该暂停谁」。
    # 挂起的任务不在表内（_continue_task 返回时已注销）——它本来就在等用户，
    # /stop 对它无事可做。
    RUNNING_TASKS: dict[str, "TaskRuntime"] = {}
    RUNNING_LOCK = threading.Lock()

    context_client = ContextClient()
    model_client = ModelProxyClient()
    tool_client = ToolRuntimeClient()

    def __init__(self, agent_id: str, session_id: str, user_id: str = "default"):
        self.id = agent_id
        self.session_id = session_id
        self.user_id = user_id or "default"
        self.config = {}
        # 构建结果整体放一个属性里：热重载（refresh_system_prompt）用**一次赋值**换掉整份，
        # 段落列表 / @when 下标 / 记录串必然同源，不会出现「新 prompt 配旧下标」的中间态。
        # 下面四个同名属性都是从它派生的只读property（context_builder 直接读它们）。
        self._prompt_parts = SystemPromptParts()
        self._prompt_revision: tuple = ()
        self.load_config()
        self.build_system_prompt()

    @property
    def system_prompt(self) -> list[dict[str, str]]:
        return self._prompt_parts.messages

    @property
    def _identity_system_indexes(self) -> list[int]:
        return self._prompt_parts.identity_indexes

    @property
    def _when_system_rules(self) -> list[tuple[int, str]]:
        """「第几段 → @when 取值」：构建期记录，发送期过滤（见 effective_system_prompt）。"""
        return self._prompt_parts.when_rules

    @property
    def _soul_message_content(self) -> str:
        return self._prompt_parts.soul_content

    @property
    def _world_info_override_content(self) -> str:
        return self._prompt_parts.world_info_override

    def build_system_prompt(self):
        """构建 system prompt（GLOBAL_SETTING / files 声明 / PROCESS / TOOL / 世界书）。

        具体拼接逻辑见 app/context_builder.py；这里只把结果存到实例上，
        供 send() 组装 messages 时使用。

        快照在**读取之前**取：万一构建过程中文件又被改了，快照会偏旧，
        下一次 refresh_system_prompt 就会再重建一遍 —— 宁可多建一次，不要漏。
        """
        stamp = prompt_revision()
        self._prompt_parts = ContextBuilder(self).build_system_prompt()
        self._prompt_revision = stamp

    def refresh_system_prompt(self) -> bool:
        """提示词文件 / 提示词套开关变了就重建；返回是否重建过。

        为什么需要它：system prompt 原本只在实例创建时构建一次，而实例按
        `(user, session, agent)` 缓存在 `_agent_instances`（LRU 20），于是
        「在 dashboard 改完提示词」或「换一套人设」之后，已经在聊的会话会一直用旧内容 ——
        要么等 LRU 把它挤掉（不同用户可能同时跑着两套人设），要么重启 orchestrator
        （而重启会杀掉在飞任务）。这一步把生效时机收敛到「下一次模型调用」。

        放在 send() 开头而不是任务入口：子智能体（tool / reader…）各有自己的实例，
        只刷 main 的话它们的提示词仍是旧的。
        """
        stamp = prompt_revision()
        if stamp == self._prompt_revision:
            return False
        # 重建失败直接抛出（套名写错 / 套目录缺失）：与 @ref= 一致，不静默用错人设。
        # 这里刻意**不**先记快照，失败时下一条消息会再报一次，直到配置被改正。
        self.build_system_prompt()
        debug_log(f"[{self.user_id}] {self.id} 提示词已变更，system prompt 重建完成")
        return True

    @staticmethod
    def _is_user_object(target, task) -> bool:
        """
        判断弹栈目标是否是当前任务的真实用户对象。

        这里保留原始版语义：user 仍然在 agent_context 栈里。
        但微服务化后，弹到 user 时不能再调用 user.send()，
        而是要 emit assistant_message，由 scheduler/gateway/frontend 负责投递。
        """
        return target is task.user or target.__class__.__name__ == "RuntimeUser"

    @staticmethod
    def _is_user_agent_id(target_agent_id: str, task) -> bool:
        """
        判断模型输出的 对话:xxx|... 是否是在给用户发消息。

        允许：
        - 对话:user|...
        - 对话:用户|...
        - 对话:<真实 user_id>|...
        """
        if target_agent_id is None:
            return False

        raw = str(target_agent_id).strip()
        normalized = raw.lower()

        return normalized in {"user", "用户"} or raw == str(task.user.id)

    @staticmethod
    def _extract_raw_model_text(model_response) -> str:
        """
        从 model-proxy 返回体里尽量提取模型原始文本。
        """
        try:
            if model_response is None:
                return ""

            if isinstance(model_response, str):
                return model_response

            if not isinstance(model_response, dict):
                return str(model_response)

            if "text" in model_response:
                return str(model_response.get("text") or "")

            if "choices" in model_response:
                choices = model_response.get("choices") or []
                if choices:
                    message = choices[0].get("message", {})
                    return str(message.get("content") or "")

            if "message" in model_response:
                message = model_response.get("message")
                if isinstance(message, dict):
                    return str(message.get("content") or "")

            return str(model_response)
        except Exception:
            return ""

    @classmethod
    def _emit_user_message(
        cls,
        task,
        emit: Callable[[TaskEventDTO], None],
        text: str,
        final: bool = True,
        agent_id: str = "",
    ) -> str:
        final_reply = "" if text is None else str(text)

        task.send_text = final_reply
        task.set_temp_dialog_output(final_reply)

        emit(cls.build_event(
            task,
            "assistant_message",
            text=final_reply,
            images=list(task.send_images),
            metadata={
                "visible_to_user": "true",
                "final": "true" if final else "false",
                "agent_id": agent_id or getattr(task, "agent_id", "") or "main",
            },
        ))

        if final:
            task.status = "completed"

        return final_reply

    @classmethod
    def _persist_tool_summary(cls, task) -> None:
        """将 tool_log 总结后持久化到 tool 的上下文，供 tool agent 后续检索。"""
        if not task.tool_log:
            return
        tool_calls = [entry for entry in task.tool_log if str(entry).startswith("结果:")]
        if len(tool_calls) < 3:
            return
        try:
            tool_log_text = "\n".join(task.tool_log)
            reader = cls.get_agent("reader", task.user.session_id, task.user.id)
            reader_profile = reader.config.get("model_profile") or reader.config.get("model") or "reader"
            summary_messages = [
                {"role": "system", "content": "请在三句话内总结以下工具执行记录，只输出总结内容。**注意内容主要反映工具如何正确使用，而非调用结果**"},
                {"role": "user", "content": tool_log_text},
            ]
            summary_resp = cls.model_client.chat_completion(
                task_id=task.task_id,
                agent_id="reader",
                model_profile=reader_profile,
                messages=summary_messages,
                params={"temperature": 0.3, "max_tokens": "256", "stream": "false"},
            )
            summary_text = summary_resp.get("text", "") or tool_log_text
            tool_agent = cls.get_agent("tool", task.user.session_id, task.user.id)
            commit_limit = int(tool_agent.config.get("commit_limit", 0) or 0)
            cls.context_client.append_turn(
                user_id=task.user.id,
                session_id=task.user.session_id,
                task_id=task.task_id,
                user_message="工具执行记录",
                assistant_message=summary_text,
                agent_id="tool",
                tool_summaries=[],
                commit_limit=commit_limit,
                max_messages=int(tool_agent.config.get("max_messages", 6) or 6),
            )
        except Exception as exc:
            debug_log(f"[{task.user.id}] tool_log summary append failed: {exc}")

    @classmethod
    def _emit_raw_model_fallback(
        cls,
        task,
        emit: Callable[[TaskEventDTO], None],
        raw_text: str,
        exc: Exception | None = None,
    ) -> str:
        """
        兜底：模型已经生成过内容，但后续解析/调度/弹栈出现异常时，
        不再让任务直接失败，而是把模型原始输出直接发给用户。
        """
        fallback_text = (raw_text or "").strip()
        if not fallback_text:
            fallback_text = f"任务处理异常：{exc}" if exc else "任务处理异常，但没有可用的模型原始输出。"

        if exc is not None:
            debug_log(f"[{task.user.id}] [fallback_raw_model_output] {exc}")

        agent_id = getattr(getattr(task, "default_agent", None), "id", "") or getattr(task, "agent_id", "") or "main"
        return cls._emit_user_message(task, emit, fallback_text, final=True, agent_id=agent_id)

    @classmethod
    def suspend_task(cls, task, emit: Callable[[TaskEventDTO], None], question: str, agent_id: str = "") -> None:
        """询问:xxx -> 挂起任务，等待用户回复后恢复。"""
        with cls.PENDING_LOCK:
            cls.PENDING_TASKS[task.task_id] = task
        emit(cls.build_event(
            task,
            "task_waiting_user",
            text="询问：" + question,
            metadata={
                "visible_to_user": "true",
                "final": "false",
                "suspend": "true",
                "agent_id": agent_id or getattr(task, "agent_id", "") or "main",
            },
        ))

    @classmethod
    def pop_pending_task(cls, task_id: str):
        """从挂起注册表取回任务；找不到返回 None（调用方回退为普通新任务）。"""
        with cls.PENDING_LOCK:
            return cls.PENDING_TASKS.pop(task_id, None)

    # ─── 运行中任务登记 + 打断（barge-in）────────────────────────
    @classmethod
    def _register_running(cls, task) -> None:
        with cls.RUNNING_LOCK:
            cls.RUNNING_TASKS[task.user.id] = task

    @classmethod
    def _unregister_running(cls, task) -> None:
        with cls.RUNNING_LOCK:
            # 只在仍指向本任务时删除：同一用户可能已经开始了新任务。
            if cls.RUNNING_TASKS.get(task.user.id) is task:
                del cls.RUNNING_TASKS[task.user.id]

    @classmethod
    def request_stop(cls, user_id: str, task_id: str = "") -> bool:
        """请求暂停某用户当前运行中的任务（协作式，立即返回）。

        只置位标志、不阻塞等待：任务在自己的检查点观察该标志后自行暂停。
        返回 False = 当时没有任务在跑（已结束或本来就空闲）。
        """
        with cls.RUNNING_LOCK:
            task = cls.RUNNING_TASKS.get(user_id)
        if task is None:
            return False
        if task_id and task.task_id != task_id:
            return False
        task.interrupt_event.set()
        debug_log(f"[{user_id}] 收到停止请求，任务 {task.task_id} 将在下一个检查点暂停")
        return True

    @classmethod
    def _suspend_for_interject(cls, task, emit: Callable[[TaskEventDTO], None]) -> bool:
        """登记为打断式暂停并发事件。调用方负责把现场摆成「可恢复」的样子。"""
        task.paused_by_interrupt = True
        task.status = "suspended"
        with cls.PENDING_LOCK:
            cls.PENDING_TASKS[task.task_id] = task
        emit(cls.build_event(
            task,
            "task_waiting_user",
            text=INTERJECT_PAUSE_TEXT,
            metadata={
                "visible_to_user": "true",
                "final": "false",
                "suspend": "true",
                # 与「询问」挂起区分：scheduler 据此登记插话暂停（并消化队列里
                # 已经排上的消息），前端也据此显示不同的提示。
                "interrupt": "true",
                "agent_id": (
                    getattr(getattr(task, "default_agent", None), "id", "")
                    or getattr(task, "agent_id", "")
                    or "main"
                ),
            },
        ))
        chat_log(
            f"[{task.user.id}] 已打断并保留现场，等待用户插话（栈长 {len(task.agent_context)}）"
        )
        return True

    @classmethod
    def _pause_for_interject(cls, task, emit: Callable[[TaskEventDTO], None]) -> bool:
        """主循环头部的检查点：挂起任务，等用户插话。

        **不销毁任何东西**——agent_context 栈、tool_log、main_memory、
        temp_dialog_* 全部原样留在同一个 TaskRuntime 上，并放进 PENDING_TASKS；
        用户下一条消息经 ResumeTask 回到同一个检查点时接着跑（见 resume_task）。

        返回 False = 不该在这里暂停：栈顶就是用户帧，下一步本来就是把最终回复
        发出去，暂停毫无意义。
        """
        if not task.agent_context:
            return False
        if cls._is_user_object(task.agent_context[-1].get("from"), task):
            return False
        return cls._suspend_for_interject(task, emit)

    @classmethod
    def _pause_before_model_call(cls, task, agent, emit: Callable[[TaskEventDTO], None]) -> bool:
        """模型调用前的检查点：不启动这次调用，就地暂停等插话。

        为什么光有主循环那个检查点不够：智能体之间是**嵌套调用**的
        （`main.send()` 内部再 `call_agent` → `tool.send()`），主循环要等整条
        嵌套链返回才轮到。实测（真实链路）单次模型调用要 40 秒以上，于是用户按了
        /stop 之后还得白等一整条链（可能几分钟）才生效。
        这个检查点只是「不再开新的模型调用」，并不打断正在跑的那一次 —— 与
        「等当前这一步跑完」的语义一致。

        现场处理：把「本来要发给这个智能体的输入」压成它自己的帧，于是恢复时
        主循环弹出这一帧、把插话并在后面交回给它，它带着原输入 + 插话重跑一次
        —— 和「询问」恢复是同一套帧语义，不需要额外状态。
        """
        pending_input = str(task.temp_dialog_input or "")
        task.push_context(agent, pending_input)
        task.set_temp_dialog_input(None)
        task.set_temp_dialog_output("")
        task.target = agent
        return cls._suspend_for_interject(task, emit)

    @classmethod
    def resume_task(
        cls,
        task,
        reply: str,
        emit: Callable[[TaskEventDTO], None],
        images: list[str] | None = None,
    ) -> str:
        """
        恢复挂起任务：与对话流程一致，把用户回复弹栈拼接后继续主循环。

        挂起时压栈的是 {from: 询问方, input: "【已发送给用户】\\n询问：xxx"}；
        恢复时弹出该上下文，把回复内容（用户回复或系统超时提示）作为
        「收到返回」拼在后面，交给询问方继续 send，再走 _continue_task 主循环。
        """
        task.status = "running"

        # 与 process_task 一致：登记必须覆盖整个执行（含恢复后的第一次模型调用），
        # 否则恢复期间到达的 /stop 找不到这个任务。
        task.interrupt_event.clear()
        cls._register_running(task)
        try:
            return cls._resume_body(task, reply, emit, images)
        finally:
            cls._unregister_running(task)

    @classmethod
    def _resume_body(cls, task, reply: str, emit: Callable[[TaskEventDTO], None], images) -> str:
        """resume_task 的本体（登记由调用方负责）。"""
        # ─── 插话恢复路径 ───────────────────────────────────────────
        # 本次挂起来自 /stop：或由任务自己暂停（paused_by_interrupt），或在
        # 「询问」挂起期间又被 /stop 标记（scheduler 打的 metadata.resume_mode）。
        # 两条来源都走这里：**不弹栈、不构造【收到返回】**——现场从没被破坏，
        # 只把插话放好，交给主循环在弹帧时并入栈顶调用者的输入。
        resume_mode = str((task.metadata or {}).get("resume_mode") or "")
        if task.paused_by_interrupt or resume_mode == "interrupt":
            task.paused_by_interrupt = False
            task.metadata.pop("resume_mode", None)
            task.pending_interjection = str(reply)
            # 插话同样并入本轮用户输入，保证持久化到上下文的这一轮里
            # 能看到用户中途说了什么（否则插话只活在当次 messages 里）。
            task.content = f"{task.content}\n\n【用户插话】\n{reply}".strip()
            if images:
                task.set_temp_dialog_images(images)
            chat_log(f"[{task.user.id}] 收到插话，接着被打断的任务继续：{reply}")
            return cls._continue_task(task, emit)

        context = task.pop_context()
        stack_input = (context or {}).get("input", "")

        parts = []
        if stack_input:
            parts.append(stack_input)
        parts.append("【收到返回】\n" + str(reply))
        task.set_temp_dialog_input("\n\n".join(parts))
        if images:
            task.set_temp_dialog_images(images)

        asking_agent = task.target
        if asking_agent is not None and hasattr(asking_agent, "send"):
            asking_agent.send(task, emit)

        return cls._continue_task(task, emit)

    @classmethod
    def _shell_allowed(cls, user_id: str) -> bool:
        """shell 白名单校验：未开启限制时所有用户可用。"""
        data = load_system_settings()
        if not bool(data.get("shell_restriction_enabled", False)):
            return True
        allowed = data.get("shell_allowed_users") or []
        allowed_set = {str(u).strip() for u in allowed if str(u).strip()}
        return str(user_id) in allowed_set

    @staticmethod
    def _is_windows_path(value) -> bool:
        """是否 Windows 盘路径或 /mnt 路径。"""
        raw = str(value or "").strip()
        if raw.startswith("/mnt/"):
            return True
        return bool(re.match(r"^[A-Za-z]:[\\/]", raw))

    @classmethod
    def _mnt_write_allowed(cls, user_id: str) -> bool:
        """/mnt 写白名单校验：未开启限制时所有用户可用。"""
        data = load_system_settings()
        if not bool(data.get("mnt_write_restriction_enabled", False)):
            return True
        allowed = data.get("mnt_write_allowed_users") or []
        allowed_set = {str(u).strip() for u in allowed if str(u).strip()}
        return str(user_id) in allowed_set

    @classmethod
    def get_agent(cls, agent_id: str, session_id: str, user_id: str = "default") -> "AgentRuntime":
        debug_log(f"[get_agent] calling with agent_id={agent_id!r} session_id={session_id!r} user_id={user_id!r}")
        user_id = user_id or "default"
        agent_id = (agent_id or "").strip() or "main"
        key = f"{user_id}_{session_id}_{agent_id}"
        if key in cls._agent_instances:
            cls._agent_instances.move_to_end(key)
            return cls._agent_instances[key]

        if len(cls._agent_instances) >= cls.MAX_INSTANCES:
            oldest_key = next(iter(cls._agent_instances))
            del cls._agent_instances[oldest_key]
            debug_log(f"[{user_id}] [实例上限] 删除最久未使用: {oldest_key}")

        try:
            agent = cls(agent_id, session_id, user_id)
        except KeyError:
            # 未知智能体：加载配置抛异常（agent_list.json 中不存在），
            # 自动把会话默认智能体换成 main 后重试。
            debug_log(f"[{user_id}] 未知智能体 {agent_id!r}，default 换为 main")
            cls.default_agent[session_id] = "main"
            agent_id = "main"
            key = f"{user_id}_{session_id}_{agent_id}"
            if key in cls._agent_instances:
                cls._agent_instances.move_to_end(key)
                return cls._agent_instances[key]
            agent = cls(agent_id, session_id, user_id)

        cls._agent_instances[key] = agent
        debug_log(f"[{user_id}] {session_id} 新建智能体: {agent_id}")
        return agent

    @classmethod
    def first_call(cls, task):
        agent_id = task.agent_id or cls.default_agent.get(task.user.session_id, "main")
        if not agent_id or not str(agent_id).strip():
            agent_id = "main"
        # 定时任务触发的执行（metadata.source == "timer_task"）：用指定 agent 处理本次任务，
        # 但**不改写会话默认智能体**——否则 submit 定时任务到点执行时会用定时任务里指定的
        # agent_id 覆盖用户（如 QQ 端）的默认智能体。
        if (task.metadata or {}).get("source") != "timer_task":
            cls.default_agent[task.user.session_id] = agent_id
        target = cls.get_agent(agent_id, task.user.session_id, task.user.id)
        task.target = target
        chat_log(f"[{task.user.id}] {task.user.session_id}->{target.id}\n{task.content}")
        debug_log(f"[{task.user.id}] [user_chat]{task.user.session_id}->{target.id}")

    @classmethod
    def process_task(cls, task, emit: Callable[[TaskEventDTO], None]) -> str:
        debug_log(f"{task.user.id} 的任务正被 orchestrator 处理")
        final_reply = ""

        # 运行中登记必须覆盖**整个执行**，包括第一次模型调用：
        # 实测（真实链路）第一次模型调用可能就要十几秒，若只在主循环里登记，
        # 用户在这段时间按 /stop 会被告知「没有正在执行的任务」—— 而任务明明在跑。
        # 顺便清掉残留标记，避免它跨「挂起→恢复」被误用。
        task.interrupt_event.clear()
        cls._register_running(task)
        try:
            emit(cls.build_event(task, "task_started"))

            if len(task.agent_context) == 1:
                try:
                    cls.first_call(task)
                    task.default_agent = task.target
                    task.default_agent.send(task, emit)
                except Exception as exc:
                    raw_output = task.consume_temp_dialog_output() or task.send_text or ""
                    return cls._emit_raw_model_fallback(task, emit, str(raw_output), exc)

            return cls._continue_task(task, emit)
        finally:
            cls._unregister_running(task)

    @classmethod
    def _continue_task(cls, task, emit: Callable[[TaskEventDTO], None]) -> str:
        """主循环 + 收尾：process_task 和 resume_task 都会走到这里。

        现场保留式暂停（/stop）的检查点就在循环头。运行中登记**不在这里**做：
        它必须覆盖整个执行（含第一次模型调用），见 process_task / resume_task。
        """
        final_reply = ""
        steps = 0
        while len(task.agent_context) > 0 and task.status == "running":
            if task.interrupt_event.is_set():
                if cls._pause_for_interject(task, emit):
                    return ""
                # 任务即将收尾（栈顶就是用户帧，下一步就是把最终回复发出去）：
                # 此时暂停没有意义，放它跑完，并清掉标记避免下一轮再触发。
                debug_log(f"[{task.user.id}] 停止请求到达时任务已近收尾，忽略")
                task.interrupt_event.clear()

            steps += 1
            if steps > config.MAX_AGENT_STEPS:
                task.status = "failed"
                final_reply = "任务执行步数过多，已中止。"
                emit(cls.build_event(task, "task_failed", error=final_reply, text=final_reply))
                return final_reply

            debug_log(f"[{task.user.id}] 弹回复栈，当前栈长 {len(task.agent_context)}")
            returning_agent_id = getattr(task.target, 'id', '')
            context = task.pop_context()
            task.target = context["from"]
            stack_input = context.get("input", "")
            raw_output = task.consume_temp_dialog_output()
            output = raw_output or "因不知名原因输出已丢失"
            # 插话（barge-in）：/stop 暂停期间用户发来的内容，并入本轮交给栈顶调用者
            interjection = task.pending_interjection
            task.pending_interjection = ""
            if task.target is not None and hasattr(task.target, 'id') and task.target.id == "main":
                task.main_memory.append(f"←{returning_agent_id}: {str(output)}")

            if cls._is_user_object(task.target, task):
                agent_id = getattr(getattr(task, "default_agent", None), "id", "") or getattr(task, "agent_id", "") or "main"
                final_reply = cls._emit_user_message(task, emit, output, final=True, agent_id=agent_id)
                break

            try:
                # 弹栈时把栈内容拼在前面，并在 output 前加【收到返回】标记
                parts = []
                if stack_input:
                    parts.append(stack_input)
                original_result = str(output)
                if interjection:
                    # 插话轮：有真实产出就照常回喂（放在插话之前，让模型先看到刚刚
                    # 发生了什么、再看用户的新要求）；占位符则不喂，避免
                    # 「输出已丢失」这种噪音干扰插话本身。
                    if raw_output:
                        parts.append("【收到返回】\n" + original_result)
                    parts.append("【用户插话】\n" + interjection)
                else:
                    parts.append("【收到返回】\n" + original_result)
                output = "\n\n".join(parts)
                task.tool_log.append("收到返回:" + original_result)
                if interjection:
                    task.tool_log.append("用户插话:" + interjection)
                task.set_temp_dialog_input(str(output))
                task.target.send(task, emit)
            except Exception as exc:
                final_reply = cls._emit_raw_model_fallback(task, emit, str(output), exc)
                break

            final_reply = str(output)

        if task.status == "suspended":
            # 询问挂起：task_waiting_user 事件已发，不补终态事件，等用户回复后恢复。
            return ""

        if task.status == "running":
            task.status = "completed"

        if not final_reply and task.send_text:
            final_reply = task.send_text

        if final_reply or task.intermediate_texts:
            try:
                default_agent = getattr(task, "default_agent", None)
                default_agent_id = getattr(default_agent, "id", "main")
                default_agent_config = getattr(default_agent, "config", {}) or {}
                record_mode = str(default_agent_config.get("record_mode", "final") or "final").strip().lower()
                if record_mode == "intermediate":
                    # 只记录第一步返回的前半段（中间推送内容）；没有则回退最终回复。
                    recorded_message = task.intermediate_texts[0] if task.intermediate_texts else final_reply
                elif record_mode == "both":
                    parts = list(task.intermediate_texts)
                    if final_reply:
                        parts.append(final_reply)
                    recorded_message = "\n\n".join(parts) if parts else final_reply
                else:
                    recorded_message = final_reply
                if not recorded_message:
                    recorded_message = final_reply
                cls.context_client.append_turn(
                    user_id=task.user.id,
                    session_id=task.user.session_id,
                    task_id=task.task_id,
                    user_message=task.content,
                    assistant_message=recorded_message,
                    agent_id=task.agent_id or default_agent_id,
                    tool_summaries=[],
                    commit_limit=int(default_agent_config.get("commit_limit", 0) or 0),
                    max_messages=int(default_agent_config.get("max_messages", 6) or 6),
                )
            except Exception as exc:
                debug_log(f"[{task.user.id}] append_turn failed: {exc}")

            cls._persist_tool_summary(task)
            task.tool_log.clear()

        emit(cls.build_event(task, "task_completed"))
        return final_reply

    @classmethod
    def build_event(cls, task, event_type: str, text: str = "", error: str = "", images=None, metadata=None) -> TaskEventDTO:
        client_message_id = task.metadata.get("client_message_id", "")
        delivery_target = DeliveryTarget(
            channel=task.channel,
            user_id=task.user.id,
            conversation_id=task.user.session_id,
            reply_to=client_message_id,
        )
        return TaskEventDTO(
            event_id=new_event_id(),
            task_id=task.task_id,
            user_id=task.user.id,
            session_id=task.user.session_id,
            channel=task.channel,
            type=event_type,
            text=text,
            images=images or [],
            error=error,
            delivery_target=delivery_target,
            metadata=metadata or {},
        )

    def set_default_agent(self, agent_id: str):
        AgentRuntime.default_agent[self.session_id] = agent_id

    def load_config(self):
        debug_log(f"[load_config] self.id={self.id!r} self.user_id={self.user_id!r}")
        if not config.AGENT_CONFIG_PATH.exists():
            raise FileNotFoundError(f"未找到智能体配置：{config.AGENT_CONFIG_PATH}")
        self.config = load_agent_config(
            path=config.AGENT_CONFIG_PATH,
            user_id=self.user_id,
            agent_id=self.id,
        )

    def send(self, task, emit: Callable[[TaskEventDTO], None]):
        # 提示词热重载：改了 .md / 换了提示词套 → 下一次模型调用就用新的。
        # 只做 stat 比对（见 prompt_revision），没变时是纯读开销，可以忽略。
        self.refresh_system_prompt()

        # 模型调用前的打断检查点。一旦开始构建上下文/调用模型，这次调用就
        # 没法再中止了（见 _pause_before_model_call 的说明）。暂停时 send() 直接返回，
        # 调用方（process_task / call_agent / 主循环）会看到 status=suspended：
        # 主循环据此不再发终态事件，嵌套链也逐层退出。
        if task.interrupt_event.is_set():
            if AgentRuntime._pause_before_model_call(task, self, emit):
                return

        content = task.consume_temp_dialog_input() or ""
        dialog_images = task.consume_temp_dialog_images() or []

        chat_log(f"[{self.user_id}] {self.id}收到:\n{content}")

        # 上下文拼接（system prompt / 长上下文 / 任务记忆 / 用户输入 / vars / 本轮输入）
        # 统一交给 context_builder；这里只负责模型档案、采样参数与调用阶段的选择。
        builder = ContextBuilder(self)
        context = builder.build_send_context(
            task,
            content=content,
            dialog_images=dialog_images,
            image_enabled=image_receive_enabled(),
        )
        messages = context.messages
        base_head = context.base_head
        base_tail = context.base_tail

        model_profile = self.config.get("model_profile") or self.config.get("model") or self.id
        base_model_profile = model_profile
        # 多模态路由：本次请求带图片时切换到 image_model_profile（如豆包多模态），纯文本保持 model_profile
        image_count = context.image_count
        if image_count > 0:
            vision_profile = self.config.get("image_model_profile")
            if vision_profile:
                chat_log(
                    f"[{self.user_id}] {self.id} 检测到 {image_count} 张图片，"
                    f"模型 {model_profile} -> {vision_profile}"
                )
                model_profile = vision_profile
        # 采样参数白名单：agent 配置里配了才透传（model_proxy 已支持透传；
        # 模型级默认放 model profile 的 model_params，agent 级此处覆盖）
        _SAMPLE_PARAM_KEYS = (
            "top_p", "top_k", "min_p", "tfs_z",
            "repetition_penalty", "repeat_last_n",
            "presence_penalty", "frequency_penalty",
            "mirostat", "mirostat_tau", "mirostat_eta",
            "seed",
        )
        params = {
            "temperature": self.config.get("temperature", 1),
            "max_tokens": self.config.get("max_tokens", 2048),
            "stream": False,
        }
        for _key in _SAMPLE_PARAM_KEYS:
            if _key in self.config and self.config[_key] is not None:
                params[_key] = self.config[_key]

        # 两阶段世界书：仅用户请求轮 + files 声明 world_info.md + 无图片（多模态轮保持单次）
        # 判定在 context_builder.build_send_context 里完成（two_phase_world_info）
        model_response = None
        if context.two_phase_world_info:
            model_response = builder.world_info_two_phase_call(
                task, base_head, base_tail, model_profile, params, content or ""
            )

        if model_response is None:
            try:
                model_response = self.model_client.chat_completion(
                    task_id=task.task_id,
                    agent_id=self.id,
                    model_profile=model_profile,
                    messages=messages,
                    params=params
                )
            except Exception as exc:
                # 防呆：agent_list 未配置 image_model_profile 时本就不会切换（model_profile==base）；
                # 若视觉模型路由调用失败（如 model_list 缺 default-main-vision 别名），
                # 回退到普通文本模型重试一次。重试保留原 messages（含 images）不做去图处理：
                # 若配置位置实际是单模态模型，API 会原样报错（如“不支持图片”），
                # 该报错即对用户的提示，不应静默吞掉。
                if model_profile == base_model_profile:
                    error_text = f"【模型请求失败】{exc}"
                    self._emit_user_message(task, emit, error_text, final=True, agent_id=self.id)
                    return
                chat_log(
                    f"[{self.user_id}] {self.id} 视觉模型({model_profile})调用失败：{exc}，"
                    f"回退到普通模型 {base_model_profile} 重试"
                )
                model_profile = base_model_profile
                try:
                    model_response = self.model_client.chat_completion(
                        task_id=task.task_id,
                        agent_id=self.id,
                        model_profile=model_profile,
                        messages=messages,
                        params=params
                    )
                except Exception as exc2:
                    error_text = f"【模型请求失败】{exc2}"
                    self._emit_user_message(task, emit, error_text, final=True, agent_id=self.id)
                    return

        raw_model_text = self._extract_raw_model_text(model_response)

        try:
            parsed = parse_model_response(self.id, model_response)
            raw_model_text = parsed.text or raw_model_text
            task.set_temp_dialog_output(parsed.text)

            parse_syntax(self, task)
            result = task.consume_temp_dialog_output()

            # 模型在指令前输出的说明文本：当本轮转入后台执行（工具/转交/询问/定时任务/世界书）时，
            # 先把说明文本作为中间消息发给用户，避免它随协议行一起被丢弃。
            prefix = (result.get("prefix") or "").strip()
            has_background_action = bool(
                result.get("tool_call")
                or result.get("agent_call")
                or result.get("question")
                or result.get("timer_task")
                or result.get("world_info_task")
            )
            if prefix and has_background_action:
                push_agent_id = (
                    task.agent_id
                    or getattr(getattr(task, "default_agent", None), "id", "")
                    or "main"
                )
                task.intermediate_texts.append(prefix)
                emit(self.build_event(
                    task,
                    "assistant_intermediate",
                    text=prefix,
                    metadata={"visible_to_user": "true", "final": "false", "agent_id": push_agent_id},
                ))

            if result.get("switch_call") and result["agent_call"]:
                debug_log(
                    f"[{self.user_id}] [switch_route] {self.id} -> {result['agent_call']['target_id']} "
                    f"content={result['agent_call']['content']}"
                )
                chat_log(
                    f"[{self.user_id}] {self.id} 切换并转交:\n "
                    f"{result['agent_call']['target_id']}|{result['agent_call']['content']}"
                )
            else:
                chat_log(f"[{self.user_id}] {self.id} 回复:\n {result['final_reply']}")

            task.set_temp_dialog_output(result["final_reply"])
            task.caller = self

            if result["tool_call"]:
                task.set_temp_dialog_output(result["tool_call"])
                self._run_tool(task, emit)

            elif result["agent_call"]:
                agent_call = result["agent_call"]
                target_agent_id = agent_call["target_id"]
                content_for_target = agent_call["content"]

                if self._is_user_agent_id(target_agent_id, task):
                    self._emit_user_message(task, emit, content_for_target, final=True, agent_id=self.id)
                    return

                task.set_temp_dialog_input(content_for_target)
                self.call_agent(target_agent_id, task, emit)

            elif result["question"]:
                question = (result["question"] or "").strip()
                if not question:
                    # 询问内容为空：无法挂起，把错误提示推回模型重新生成询问。
                    # 连续多次仍为空则放弃，避免死循环。
                    retry = getattr(task, "question_retry", 0)
                    if retry >= 2:
                        self._emit_user_message(
                            task, emit,
                            "出现某些问题，询问无法发送，请重新发送",
                            final=True,
                            agent_id=self.id,
                        )
                        return
                    task.question_retry = retry + 1
                    task.set_temp_dialog_input("【系统提示】出现某些问题，询问无法发送，请重新发送")
                    task.set_temp_dialog_output("")
                    self.send(task, emit)
                    return
                # 挂起等待用户回复：与对话流程类似，把询问内容压栈；
                # 用户回复（或超时后系统替用户回复）会通过 ResumeTask 弹栈拼接继续。
                task.push_context(self, "【已发送给用户】\n询问：" + question)
                task.set_temp_dialog_output("")
                task.status = "suspended"
                AgentRuntime.suspend_task(task, emit, question, agent_id=self.id)
                return

            elif result["timer_task"]:
                timer = result["timer_task"]
                client = TimerTaskClient()
                task_type = timer.get("task_type", "submit_task")

                if task_type == "delete":
                    resp = client.delete_user_task(
                        user_id=task.user.id,
                        task_id=timer.get("content", ""),
                    )
                    reply = resp["message"]
                    task.set_temp_dialog_output(reply)

                elif task_type == "query":
                    query_user_id = timer.get("content", "") or task.user.id
                    resp = client.list_user_tasks(user_id=query_user_id)
                    if resp["ok"] and not resp["tasks"] and query_user_id != task.user.id:
                        # 兼容：模型填的 user_id 未命中任何任务时，回退查询当前用户
                        query_user_id = task.user.id
                        resp = client.list_user_tasks(user_id=query_user_id)
                    if resp["ok"]:
                        tasks = resp["tasks"]
                        if not tasks:
                            reply = "当前没有定时任务"
                        else:
                            lines = ["当前定时任务列表："]
                            for t in tasks:
                                sch = t.get("schedule_str", "")
                                repeat_tag = f" | 重复:{sch}" if sch else ""
                                lines.append(
                                    f"  - [{t.get('task_id','')}] {t.get('task_type','')} "
                                    f"| {t.get('content','')} | {t.get('trigger_time_str','')}"
                                    f"{repeat_tag}"
                                )
                            reply = "\n".join(lines)
                    else:
                        reply = f"查询定时任务失败：{resp['message']}"
                    task.set_temp_dialog_output(reply)

                else:
                    # submit_task / send_message
                    timer_agent_id = timer.get("agent_id", "") or getattr(self, "id", "")
                    timer_session_id = f"{task.channel}_{task.user.id}"
                    resp = client.create_timer_task(
                        user_id=task.user.id,
                        session_id=timer_session_id,
                        channel_id=task.channel,
                        trigger_timestamp=timer.get("trigger_timestamp", 0),
                        time_str=timer.get("time_str", ""),
                        repeat_str=timer.get("repeat_str", ""),
                        content=timer.get("content", ""),
                        task_type=task_type,
                        agent_id=timer_agent_id,
                    )
                    if resp["ok"]:
                        reply = f"定时任务已创建：{resp['message']}"
                    else:
                        reply = f"定时任务创建失败：{resp['message']}"
                    task.set_temp_dialog_output(reply)

                # ── 定时任务结果回喂（不要直接作为最终回复发给用户）──
                # 与 tool_call 同机制：把执行结果压帧，由 _continue_task 弹帧时
                # 回喂本智能体再跑一轮。这样模型能看到结果并继续决策
                # （如 query 拿到列表 → 定位 task_id → 再发 delete），
                # 最终输出面向用户的自然语言时才结束。
                # 若不压帧，结果会经 user 帧直接 emit 成给用户的回复，
                # 模型拿不到结果，多步操作（查→删）会中断在半路。
                task.tool_log.append("定时任务结果:" + str(reply))
                task.push_context(self, "")
                task.set_temp_dialog_output("【定时任务执行结果】\n" + str(reply))

            elif result["world_info_task"]:
                wi_task = result["world_info_task"]
                try:
                    reply = self._handle_world_info_task(task, wi_task)
                except Exception as exc:
                    debug_log(f"[{self.user_id}] world_info 命令执行失败: {exc}")
                    reply = f"世界书命令执行失败：{exc}"
                task.set_temp_dialog_output(reply)

            else:
                final_reply = result["final_reply"]
                task.set_temp_dialog_output(final_reply)

        except Exception as exc:
            self._emit_raw_model_fallback(task, emit, raw_model_text, exc)
            return

    def _handle_world_info_task(self, task, wi_task: dict) -> str:
        """执行 世界书:add/list/update/delete/enable 协议命令，返回用户可见结果。

        scope 默认当前 agent（agent 写只对自己）；群组用 group:群组id（groups.json 定义）。
        """
        action = wi_task.get("action", "")
        args = wi_task.get("args") or []
        agent_id = self.id

        if action == "list":
            return world_info_manager.list_all(agent_id=agent_id)

        if action == "add":
            if len(args) < 2:
                return "世界书:add 用法：世界书:add|关键词1,关键词2|内容|优先级|scope|constant|regex|match_mode|exclude"
            keys_str = args[0]
            content = args[1]
            priority = args[2] if len(args) > 2 else "0"
            scope = args[3] if len(args) > 3 else ""
            constant = args[4] if len(args) > 4 else "false"
            regex = args[5] if len(args) > 5 else "false"
            match_mode = args[6] if len(args) > 6 else "or"
            exclude = args[7] if len(args) > 7 else ""
            return world_info_manager.add(
                agent_id=agent_id,
                keys_str=keys_str,
                content=content,
                priority=priority,
                scope=scope,
                constant=constant,
                regex=regex,
                match_mode=match_mode,
                exclude=exclude,
            )

        if action == "update":
            if len(args) < 2:
                return "世界书:update 用法：世界书:update|条目id|新内容"
            return world_info_manager.update(entry_id=args[0], new_content=args[1])

        if action == "delete":
            if len(args) < 1:
                return "世界书:delete 用法：世界书:delete|条目id"
            return world_info_manager.delete(entry_id=args[0])

        if action == "enable":
            if len(args) < 2:
                return "世界书:enable 用法：世界书:enable|条目id|true|false"
            return world_info_manager.enable(entry_id=args[0], enabled_str=args[1])

        return f"未知的世界书命令：{action}（支持 add/list/update/delete/enable）"

    def call_agent(self, target_agent_id: str, task, emit: Callable[[TaskEventDTO], None]):
        content = task.consume_temp_dialog_input()

        if not target_agent_id or not str(target_agent_id).strip():
            debug_log(f"[{self.user_id}] call_agent ignored: empty target_agent_id from {self.id}")
            return

        if self._is_user_agent_id(target_agent_id, task):
            self._emit_user_message(task, emit, content, final=True, agent_id=self.id)
            return

        task.last_dialog_content = content or ""
        task.push_context(self, "【已发送给" + target_agent_id + "】\n" + content)
        if self.id == "main":
            task.main_memory.append(f"→{target_agent_id}: {content}")
        chat_log(f"[{self.user_id}] <{self.session_id}>:{self.id}->{target_agent_id}\n{content}")
        debug_log(f"[{self.user_id}] [agent_call] <{self.session_id}>:{self.id}->{target_agent_id}")
        task.target = AgentRuntime.get_agent(target_agent_id, self.session_id, task.user.id)
        task.caller = self
        task.set_temp_dialog_input(content)
        task.target.send(task, emit)

    def _run_tool(self, task, emit: Callable[[TaskEventDTO], None]):
        tool_call = task.consume_temp_dialog_output()
        if not tool_call:
            task.set_temp_dialog_output("没有可执行的工具指令")
            return

        tool_name = tool_call["tool"]
        args = tool_call["args"]
        debug_log(f"[{self.user_id}] [工具执行] {self.id} → {tool_name} {args}")

        # shell 白名单拦截：dashboard「杂项」可配置哪些 user_id 能用 shell
        if tool_name in self.SHELL_TOOL_NAMES and not self._shell_allowed(task.user.id):
            block_msg = f"工具 {tool_name} 被拦截：当前用户没有使用 shell 工具的权限。"
            debug_log(f"[{task.user.id}] [shell拦截] {tool_name} 被拦截，用户未在白名单内")
            emit(self.build_event(
                task,
                "assistant_intermediate",
                text=block_msg,
                metadata={"visible_to_user": "false", "final": "false"},
            ))
            task.tool_log.append("结果:" + block_msg)
            task.set_temp_dialog_output(block_msg)
            return

        # /mnt 写白名单拦截：写类工具 + Windows 盘 / /mnt 路径
        if tool_name in self.MNT_WRITE_TOOLS and not self._mnt_write_allowed(task.user.id):
            if any(self._is_windows_path(a) for a in args):
                block_msg = f"工具 {tool_name} 被拦截：当前用户没有 /mnt 写入权限。"
                debug_log(f"[{task.user.id}] [/mnt拦截] {tool_name} 被拦截，用户未在白名单内")
                emit(self.build_event(
                    task,
                    "assistant_intermediate",
                    text=block_msg,
                    metadata={"visible_to_user": "false", "final": "false"},
                ))
                task.tool_log.append("结果:" + block_msg)
                task.set_temp_dialog_output(block_msg)
                return

        emit(self.build_event(
            task,
            "assistant_intermediate",
            text=f"正在执行工具：{tool_name}",
            metadata={"visible_to_user": "false", "final": "false"},
        ))

        # 关键修改：
        # 把 user_id/session_id 传给 tool-runtime client。
        # tool_runtime_client.py 会把 workspace_dir 构造成：
        #   /app/workspace/users/<user_id>
        # 而不是：
        #   /app/workspace/tasks/<task_id>
        # agent_id 供 process-* 等工具定位「当前对话主智能体」的存储文件。
        push_agent_id = (
            task.agent_id
            or getattr(getattr(task, "default_agent", None), "id", "")
            or "main"
        )
        tool_events = self.tool_client.execute_tool(
            task_id=task.task_id,
            tool_name=tool_name,
            args=args,
            user_id=task.user.id,
            session_id=task.user.session_id,
            agent_id=push_agent_id,
        )

        output = ""
        error = ""
        saw_done = False

        for event in tool_events:
            event_type = event.get("event_type", "")

            # 执行过程中的即时推送：立即转发给用户（不等待模型最终回复）。
            if event_type == "message":
                emit(self.build_event(
                    task,
                    "assistant_message",
                    text=event.get("text", ""),
                    metadata={
                        "visible_to_user": "true",
                        "final": "false",
                        "agent_id": push_agent_id,
                    },
                ))
                continue

            if event_type == "image":
                artifact = event.get("artifact") or {}
                asset_url = artifact.get("asset_url", "")
                emit(self.build_event(
                    task,
                    "assistant_message",
                    text=event.get("text", ""),
                    images=[asset_url] if asset_url else [],
                    metadata={
                        "visible_to_user": "true",
                        "final": "false",
                        "agent_id": push_agent_id,
                    },
                ))
                continue

            if event_type == "done":
                saw_done = True
                output = event.get("output", "")
                error = event.get("error", "")
                for artifact in event.get("artifacts", []):
                    if artifact.get("asset_url"):
                        task.send_images.append(artifact["asset_url"])

        if not saw_done:
            error = error or "tool-runtime 未返回最终结果"

        if error:
            output = f"工具执行失败：{error}"

        if not output and not error:
            output = "（工具无输出）"

        task.tool_log.append("结果:" + str(output))
        task.set_temp_dialog_output(output)
        chat_log(f"[{task.user.id}] {self.id} 执行工具 {tool_name}:\n结果: {output}")
        debug_log(f"[{task.user.id}] [工具结果] {self.id} {output}")

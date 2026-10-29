import re
import threading
import time
from collections import OrderedDict
from typing import Callable

from app import config
from app import world_info_manager
from app import mode as mode_switch
from app import user_commands
from app.agent_config import load_agent_config
from app.context_builder import (
    GROUP_SESSION_PREFIXES,
    ContextBuilder,
    SystemPromptParts,
    image_receive_enabled,
    load_system_settings,
    prompt_revision,
)
from app.context_client import AUTHOR_IN_CONTENT_KEY, ContextClient
from app.events import TaskEventDTO, DeliveryTarget, new_event_id
from app.image_store import image_marker, store_images
from app.logger import chat_log, debug_log
from app.model_proxy_client import ModelProxyClient
from app.prompt_plan import PromptPlanError, parse_prompt_plan
from app.response_parser import parse_model_response
from app.syntax_parser import (
    has_misformatted_directive,
    parse_syntax,
    strip_self_prefix,
    render_dispatch_memory,
    render_return_memory,
)
from app.tool_runtime_client import ToolRuntimeClient
from app.timer_task_client import TimerTaskClient


# /stop 打断后的用户可见提示。措辞上必须点明「进度已保留」：
# 打断是暂停而不是销毁，用户看到这句才敢直接接着说下一句。
INTERJECT_PAUSE_TEXT = "⏹ 已停止。当前进度已保留，直接发消息即可接着继续。"

# 正文首部是否已有"权威归属"：可带时间戳，形如 `[10-28 20:45] <main>:`
# 与 context_builder._SPEAKER_PREFIX_RE / text_utils.AUTHOR_PREFIX_RE 的口径保持一致。
_HAS_SPEAKER_PREFIX_RE = re.compile(r"^\s*(\[[^\]\n]{1,32}\]\s*)?<[^>\n]{1,64}>\s*[:：]")


def should_write_return_log(frame_kind: str) -> bool:
    """弹栈时是否把产出记成 `回复:`（写进任务级 tool_log）。

    只有 `dialog` 帧（`对话:` 转交）的产出是**对方交回的回复**。其余帧各有自己的
    记录位置：工具结果 → `结果:`（`_run_tool` 里写）、定时任务结果 → `定时任务结果:`、
    询问/插话 → 自己继续跑。再记一遍就是双写，也是坏格式（DSML 之类）被回灌的入口。
    """
    return frame_kind == "dialog"


def should_record_main_return(frame_kind: str, target_id: str | None) -> bool:
    """弹栈时是否把这次返回记进 main 的「本次任务记忆」。"""
    return frame_kind == "dialog" and target_id == "main"


def record_dispatch(task, target_agent_id: str, content, caller_id: str) -> None:
    """派发（`对话:`）时只记 main 的「本次任务记忆」：`【已转交 X】<call_input>`。

    `tool_log` 的 `收到请求:` **不在这里写** —— 它只属于"工作回合"：请求随 `dialog` 帧挂着
    （`push_context(..., request=…)`），等这一轮真的调了工具，才由
    `TaskRuntime.begin_work_episode()` 补记。纯对话转交（对方只回一句话、没调工具）
    不进 `tool_log`，否则工具记录里会混进聊天内容。

    刻意**不**把 `→tool: …` 这种写法写进记忆：会和 `对话:` 协议撞车，模型会把
    记忆里的渲染当成替代语法（见 syntax_parser 顶部的伪指令注释）。
    """
    text = "" if content is None else str(content)
    if caller_id == "main":
        task.main_memory.append(render_dispatch_memory(target_agent_id, text))


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
    # 模式开关（与 default_agent 并列）：session_id → 模式名，缺省 default。
    # 语义：切换 agent 时重置为 default；模式不动 agent_id，所以上下文共用。
    default_mode: dict[str, str] = {}

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
        self._config_revision_stamp = self._config_revision()
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

    @property
    def _tool_system_indexes(self) -> list[int]:
        """TOOL 段下标（`exclude_block: ["tool"]` 用）。"""
        return self._prompt_parts.tool_indexes

    @property
    def _global_system_index(self) -> int:
        """全局设定段下标（-1 = 缺席）；锚点 before_persona 靠它定位"人设之前"。"""
        return self._prompt_parts.global_index

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
        # 记住这次构建用的模式：模式变了即使提示词文件没变也要重建
        self._prompt_mode = self.current_mode()

    @staticmethod
    def _config_revision() -> tuple:
        """agent_list.json 的廉价版本戳（mtime + 大小）。"""
        try:
            stat = config.AGENT_CONFIG_PATH.stat()
            return (stat.st_mtime_ns, stat.st_size)
        except OSError:
            return (0, 0)

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
        config_stamp = self._config_revision()
        config_changed = config_stamp != getattr(self, "_config_revision_stamp", (0, 0))
        mode_changed = self.current_mode() != getattr(self, "_prompt_mode", "")
        if stamp == self._prompt_revision and not config_changed and not mode_changed:
            return False
        if config_changed:
            # agent_list.json 改过（新增 modes / 换模型 / 改 files）：先重载配置再重建提示词，
            # 否则已有实例会一直用旧配置（只有新建实例才吃到）
            self.load_config()
            self._config_revision_stamp = config_stamp
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
    def set_session_state(
        cls,
        user_id: str,
        session_id: str,
        agent_id: str = "",
        mode: str = "",
    ) -> tuple[bool, str, str]:
        """斜杠命令的服务端实现（/agent、/mode）。

        与 `/stop` 同一层：scheduler 在任务入口拦下命令，经 RPC 调到这里，
        只改内存里的 `default_agent` / `default_mode`，不建实例、不写历史、不调模型。

        返回 `(ok, message, error)`；message 是给用户的回执原文。
        """
        uid = str(user_id or "default")
        sid = str(session_id or "")
        current_agent = cls.default_agent.get(sid, "main") or "main"
        current_mode = mode_switch.normalize_mode(cls.default_mode.get(sid))
        names = user_commands.available_agents(config.AGENT_CONFIG_PATH, uid)

        def _agent_config(agent: str) -> dict:
            try:
                return load_agent_config(
                    path=config.AGENT_CONFIG_PATH, user_id=uid, agent_id=agent
                )
            except Exception as exc:
                debug_log(f"[{uid}] 读取智能体 {agent} 配置失败：{exc}")
                return {}

        if agent_id:
            if not user_commands.agent_exists(config.AGENT_CONFIG_PATH, uid, agent_id):
                return False, "", f"未知智能体 {agent_id!r}，可用：{', '.join(names) or '（无）'}"
            cls.default_agent[sid] = agent_id
            # 换 agent 即换人：模式回到 default
            cls.default_mode[sid] = mode_switch.DEFAULT_MODE
            debug_log(f"[{uid}] 斜杠命令切换智能体 -> {agent_id}（模式重置 default）")
            return True, f"已切换智能体：{agent_id}（模式重置为 default）", ""

        if mode:
            agent_config = _agent_config(current_agent)
            error = mode_switch.validate_mode(
                agent_config,
                config.SYSTEM_PROMPT_DIR / current_agent,
                mode,
            )
            if error:
                return False, "", error
            canonical = mode_switch.canonical_mode(agent_config, mode)
            cls.default_mode[sid] = canonical
            effective = mode_switch.effective_config(agent_config, canonical)
            debug_log(
                f"[{uid}] 斜杠命令切换模式 -> {canonical}"
                f"（模型 {effective.get('model_profile')}）"
            )
            return True, (
                f"已切换模式：{canonical}（智能体 {current_agent}，"
                f"模型 {effective.get('model_profile', '')}）"
            ), ""

        # 无参数 = 查看当前状态
        agent_config = _agent_config(current_agent)
        modes = mode_switch.available_modes(agent_config)
        return True, (
            f"当前智能体：{current_agent}｜当前模式：{current_mode}\n"
            f"可用智能体：{', '.join(names) or '（无）'}\n"
            f"可用模式（{current_agent}）：{', '.join(modes)}\n"
            "切换：/agent <名称>、/mode <名称>（/mode default 回默认）"
        ), ""

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
    def _store_incoming_images(cls, task, images) -> None:
        """把本轮收到的图片自动存进用户工作区，并记下引用（见 app/image_store.py）。

        这是**框架行为**，不是工具：模型不需要、也无法主动"保存"用户发来的图。
        落盘成功 → `task.image_refs` 多一条 `uploads/images/<日期>/<hash>.<ext>`，
        提示词与本轮历史都会带上它；之后任何一轮都能用 `image-view` 回看。

        失败闭合：单张存不下来只记日志并跳过，绝不影响本轮对话。
        """
        values = [str(v) for v in (images or []) if v]
        if not values:
            return
        # 杂项「图像接收」关掉时，图片与消息里那句「图片已被忽略」保持一致：
        # 不落盘、不产生引用（否则模型会拿到路径，反而绕过这个开关）。
        if not image_receive_enabled():
            debug_log(f"[{task.user.id}] 图像接收已关闭，跳过图片落盘 {len(values)} 张")
            return
        try:
            refs = store_images(config.WORKSPACE_DIR, task.user.id, values)
        except Exception as exc:
            debug_log(f"[{task.user.id}] 图片落盘失败（已跳过）: {exc}")
            return
        for ref in refs:
            if ref not in task.image_refs:
                task.image_refs.append(ref)
        if len(refs) < len(values):
            debug_log(
                f"[{task.user.id}] 图片落盘部分成功 {len(refs)}/{len(values)}"
                f"（非 data URL 或格式不支持的单张已跳过）"
            )
        if refs:
            chat_log(f"[{task.user.id}] 图片已存入工作区: {', '.join(refs)}")

    @staticmethod
    def _history_user_message(task) -> str:
        """写进 openviking 的用户消息：原文 + 图片引用。

        引用必须进历史，否则下一轮模型既看不到图、也不知道这张图存在
        （历史只有纯文本，图像字节不会跟着走）。
        """
        return str(task.content or "") + image_marker(getattr(task, "image_refs", None))

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
        task.push_context(agent, pending_input, kind="interject")
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
                cls._store_incoming_images(task, images)
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
            cls._store_incoming_images(task, images)
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

        # 结构化调用（文字 RPG）：用户输入里是一段 JSON（见 app/prompt_plan.py）。
        # 解析成功 → 计划挂到 task 上（组装期读它做块屏蔽 / 四锚点插入），并把 JSON
        # 指令区从 task 的三份副本里整体摘掉 → 模型与历史都只看到 `text`。
        # 解析不出（普通聊天）→ 返回 None，什么都不做。
        # 确认是结构化调用但不合法 → 抛 PromptPlanError，由 process_task **显式失败**。
        plan = parse_prompt_plan(task.content)
        if plan is not None:
            task.prompt_plan = plan
            task.replace_input_content(plan.text)
            debug_log(
                f"[{task.user.id}] [prompt_plan] 屏蔽={list(plan.exclude_block) or '无'} "
                f"插入={sum(len(items) for items in plan.inserts.values())}段 "
                f"在场={list(plan.present) or '无'}"
            )

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

            # 带图消息：先把图片存进用户工作区（框架行为，不是工具）。
            # 必须在第一次模型调用**之前**做 —— 提示词里的 `（附图: 路径）`
            # 与本轮 history 的引用都依赖 image_refs 已经就绪。
            if getattr(task, "images", None):
                cls._store_incoming_images(task, task.images)

            if len(task.agent_context) == 1:
                try:
                    cls.first_call(task)
                    task.default_agent = task.target
                    task.default_agent.send(task, emit)
                except PromptPlanError as exc:
                    # 结构化调用不合法：**显式失败**。
                    # 这个 except 必须排在通用 except 之前 —— 否则会被下面的 raw fallback
                    # 吞成"任务处理异常"，写外部程序的那个 AI 会完全没有线索。
                    # 此处 task.content 仍是原始 JSON（替换只在解析成功后发生），
                    # 所以把原文一起回给调用方，便于定位。
                    reason = f"结构化调用不合法：{exc}"
                    debug_log(f"[{task.user.id}] {reason}")
                    task.status = "failed"
                    emit(cls.build_event(
                        task,
                        "task_failed",
                        error=reason,
                        text=f"{reason}\n原文（前 300 字）：{str(task.content)[:300]}",
                    ))
                    return reason
                except Exception as exc:
                    raw_output = task.consume_temp_dialog_output() or task.send_text or ""
                    return cls._emit_raw_model_fallback(task, emit, str(raw_output), exc)

            return cls._continue_task(task, emit)
        finally:
            cls._unregister_running(task)

    @classmethod
    def _record_callee_turn(cls, task, agent_id: str, dispatch_text: str, output: str) -> bool:
        """把"被调用智能体这一轮"写进**它自己**的记忆（用户调用同口径）。

        - 跳过 `INTER_AGENT_MEMORY_SKIP_AGENTS`（默认 tool,reader：它们是"手"和"检索器"）
        - 群共享 session（grp_/qq_g_）跳过：写入会污染共享历史（与扇出退化逻辑一致）
        - 只记被调用者；调用者的记录仍由任务收尾那一处负责
        - 幂等：弹栈只发生一次；挂起/恢复不重写
        """
        agent_id = str(agent_id or "").strip()
        dispatch_text = str(dispatch_text or "").strip()
        output = str(output or "").strip()
        if not agent_id or not dispatch_text or not output:
            return False
        if agent_id in config.INTER_AGENT_MEMORY_SKIP_AGENTS:
            return False
        session_id = str(getattr(task.user, "session_id", "") or "")
        if session_id.startswith(GROUP_SESSION_PREFIXES):
            debug_log(f"[{task.user.id}] [callee_memory] 群共享 session，跳过 {agent_id} 的回合记录")
            return False

        try:
            callee = cls.get_agent(agent_id, task.user.session_id, task.user.id)
            callee_config = getattr(callee, "config", {}) or {}
            ok = cls.context_client.append_turn(
                user_id=task.user.id,
                session_id=task.user.session_id,
                task_id=task.task_id,
                user_message=dispatch_text,
                assistant_message=output,
                agent_id=agent_id,
                tool_summaries=[],
                commit_limit=int(callee_config.get("commit_limit", 0) or 0),
                max_messages=int(callee_config.get("max_messages", 6) or 6),
            )
        except Exception as exc:  # noqa: BLE001
            debug_log(f"[{task.user.id}] [callee_memory] {agent_id} 写入失败: {exc}")
            return False
        debug_log(f"[{task.user.id}] [callee_memory] {agent_id} 回合已记录 ok={ok}")
        return bool(ok)

    @classmethod
    def _append_turn_for_recipients(
        cls,
        task,
        *,
        recorded_message: str,
        speaker_id: str,
        commit_limit: int = 0,
        max_messages: int = 6,
    ) -> None:
        """把这一轮写进短期记忆（openviking）。

        - **普通聊天**（没有结构化调用计划）：只写一次，行为与改造前完全一致。
        - **结构化调用**（文字 RPG）：按在场列表**逐人各写一份** —— 每个在场 NPC 一份
          自己的 session；正文首部写**真实发言者**并声明 `author_in_content`，
          存储层因此不会再补一次前缀（否则会变成 `<收件人>: <发言者>: …`，
          而读取侧只取最外层前缀 → 人人都把别人的话当成自己说的）。

        在场列表由外部程序给出。**发言者永远在内**（漏写会导致 NPC 不记得自己刚说的话）；
        列表为空 → 失败闭合，只写发言者自己。
        """
        plan = task.prompt_plan
        if plan is None:
            cls.context_client.append_turn(
                user_id=task.user.id,
                session_id=task.user.session_id,
                task_id=task.task_id,
                user_message=cls._history_user_message(task),
                assistant_message=recorded_message,
                agent_id=speaker_id,
                tool_summaries=[],
                commit_limit=commit_limit,
                max_messages=max_messages,
            )
            return

        recipients = [speaker_id]
        for name in plan.present:
            name = str(name or "").strip()
            if name and name not in recipients:
                recipients.append(name)

        # session 若带 grp_ / qq_g_ 前缀，存储层（VikingStore.full_session_id）会**强制共享**
        # 同一个 session。此时扇出不再是"每人一份"，而是把同一轮写进去 k 次（内容完全相同），
        # 只在共享历史里留一堆重复记录。所以这种情况只写一次。
        # 注意：这里**不报错** —— 隔离失效可以接受，块屏蔽与插入必须照常生效（注入优先）。
        if str(getattr(task.user, "session_id", "") or "").startswith(GROUP_SESSION_PREFIXES):
            if len(recipients) > 1:
                debug_log(
                    f"[{task.user.id}] [prompt_plan] session 为群共享（{task.user.session_id}），"
                    f"扇出退化为单次写入（按 agent 隔离已失效）"
                )
            recipients = recipients[:1]

        failed: list[str] = []
        for recipient in recipients:
            try:
                ok = cls.context_client.append_turn(
                    user_id=task.user.id,
                    session_id=task.user.session_id,
                    task_id=task.task_id,
                    user_message=cls._history_user_message(task),
                    assistant_message=f"<{speaker_id}>: {recorded_message}",
                    agent_id=recipient,
                    tool_summaries=[],
                    commit_limit=commit_limit,
                    max_messages=max_messages,
                    metadata={AUTHOR_IN_CONTENT_KEY: "true"},
                )
            except Exception as exc:
                ok = False
                debug_log(f"[{task.user.id}] [prompt_plan] 扇出写入异常 {recipient}: {exc}")
            if not ok:
                failed.append(recipient)

        if failed:
            # 某一份写失败 = 这个 NPC 的记忆与在场其他人**分叉**（后续剧情会互相矛盾）。
            # 绝不能静默：至少让它进日志可查。（openviking 不可用时 append_turn 会降级
            # 到本地存储并返回 False，所以这里也会被记上。）
            debug_log(
                f"[{task.user.id}] [prompt_plan] 扇出写入未成功 {len(failed)}/{len(recipients)}："
                f"{'、'.join(failed)}（openviking 可能已降级到本地存储）"
            )

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
            frame_kind = (context or {}).get("kind", "unknown")
            frame_has_work = bool((context or {}).get("work"))
            task.target = context["from"]
            stack_input = context.get("input", "")
            raw_output = task.consume_temp_dialog_output()
            output = raw_output or "因不知名原因输出已丢失"
            # 插话（barge-in）：/stop 暂停期间用户发来的内容，并入本轮交给栈顶调用者
            interjection = task.pending_interjection
            task.pending_interjection = ""
            # 只有 `dialog` 帧（`对话:` 转交）的产出才是**对方交回的回复**。
            # 工具帧的产出是工具结果（`_run_tool` 已记 `结果:`）、定时任务帧的结果
            # 已在 `定时任务结果:` 里、询问/插话帧是自己继续跑 —— 都不该再记成"返回"。
            if frame_kind == "unknown":
                debug_log(f"[{task.user.id}] [frame] 未标注 kind 的帧（来自 {returning_agent_id}），按非对话帧处理")
            if should_record_main_return(frame_kind, getattr(task.target, "id", None)):
                task.main_memory.append(render_return_memory(returning_agent_id, str(output)))

            # 被调用者（非 tool/reader）这一轮写进它自己的记忆：下次被叫时才有连续性
            if frame_kind == "dialog":
                cls._record_callee_turn(
                    task,
                    agent_id=returning_agent_id,
                    dispatch_text=(context or {}).get("dispatch") or (context or {}).get("request") or "",
                    output=str(raw_output or ""),
                )

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
                # 只有 `dialog` 帧把产出记成"回复"（工具帧/定时任务帧的结果
                # 各自已有记录位置，重复写入就是双写，也是坏格式被回灌的入口）
                # 只有"工作回合"才记回复：纯对话转交（一次工具都没调）不进 tool_log
                if should_write_return_log(frame_kind) and frame_has_work:
                    task.tool_log.append("回复:" + original_result)
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
                # 结构化调用（文字 RPG）：写进历史的内容必须是**发言者本轮自己的产出**。
                # 复用既有的 record_mode 优先级，只是强制走 intermediate —— 因为 final_reply
                # 可能是下游 agent 的输出或框架回执（"已添加世界书条目…"），把它当成发言者
                # 说的话写进共享历史，等于替 NPC 说了它没说过的話，而群内每个在场 NPC 都会
                # 读到这句。（intermediate_texts 就是"协议指令之前说的那段话"，见 send()。）
                if task.prompt_plan is not None:
                    record_mode = "intermediate"
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
                cls._append_turn_for_recipients(
                    task,
                    recorded_message=recorded_message,
                    speaker_id=task.agent_id or default_agent_id,
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
        # 换 agent 即换人：模式回到 default（用户约定）
        AgentRuntime.default_mode[self.session_id] = mode_switch.DEFAULT_MODE

    def set_default_mode(self, mode_name: str | None):
        """切换本会话的模式（不改 agent_id → 会话 key 不变 → 上下文共用）。"""
        AgentRuntime.default_mode[self.session_id] = mode_switch.canonical_mode(
            self.config or {}, mode_name
        )

    def current_mode(self) -> str:
        return mode_switch.normalize_mode(
            AgentRuntime.default_mode.get(self.session_id)
        )

    def effective_config(self) -> dict:
        """agent 配置叠加当前模式的覆盖（files / model_profile / temperature …）。"""
        return mode_switch.effective_config(self.config or {}, self.current_mode())

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

        effective = self.effective_config()
        model_profile = effective.get("model_profile") or effective.get("model") or self.id
        base_model_profile = model_profile
        # 多模态路由：**别一有图就切**。
        #   explicit（默认）：只有本轮输入自带图片（用户刚发的图，"开头"这一轮），
        #                    或本轮显式调用了 image-view（model_image 回填）才切视觉模型；
        #                    历史里残留的图片不再让每一轮都走视觉模型。
        #   auto：旧行为（上下文里只要 image_count > 0 就切）。
        # 文本里出现图片链接不算视觉请求 —— 链接只是文字，除非模型主动调 image-view。
        image_count = context.image_count
        fresh_images = len(dialog_images)
        explicit_vision = str(task.metadata.pop("_vision_explicit", "0")) == "1"
        vision_reason = ""
        if fresh_images:
            vision_reason = f"本轮新增 {fresh_images} 张图"
        elif explicit_vision:
            vision_reason = "显式调用 image-view"
        if config.VISION_ROUTE_MODE == "auto":
            if image_count > 0:
                vision_reason = vision_reason or f"上下文共 {image_count} 张图（auto 模式）"
        elif not vision_reason:
            image_count = 0  # explicit 模式下不因历史图片切换；仅用于日志口径
        if vision_reason:
            vision_profile = effective.get("image_model_profile")
            if vision_profile:
                chat_log(
                    f"[{self.user_id}] {self.id} 视觉路由（{vision_reason}），"
                    f"模型 {model_profile} -> {vision_profile}"
                )
                model_profile = vision_profile
            else:
                debug_log(
                    f"[{self.user_id}] {self.id} 需要视觉模型（{vision_reason}），"
                    "但 agent_list 未配置 image_model_profile，沿用文本模型"
                )
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
            "temperature": effective.get("temperature", 1),
            "max_tokens": effective.get("max_tokens", 2048),
            "stream": False,
        }
        for _key in _SAMPLE_PARAM_KEYS:
            if _key in effective and effective[_key] is not None:
                params[_key] = effective[_key]

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
            # 模型会模仿历史格式，给自己产出加首段（`[10-28 23:09] <main>: …`）。
            # 那个时间戳是它编的（定时任务轮尤其不对），只清**自己**的归属，别人的引用不动。
            cleaned_text = strip_self_prefix(raw_model_text, self.id)
            if cleaned_text != raw_model_text:
                debug_log(
                    f"[{self.user_id}] [prefix] {self.id} 去掉了自己写的首段"
                    f"（{raw_model_text[:24]!r} → {cleaned_text[:24]!r}）"
                )
                raw_model_text = cleaned_text
            task.set_temp_dialog_output(cleaned_text)

            parse_syntax(self, task)
            result = task.consume_temp_dialog_output()

            # 模式切换（模型输出的指令）：只换提示词与模型，不动 agent/会话 key。
            mode_name = str((result or {}).get("mode_switch") or "").strip()
            if mode_name:
                error = mode_switch.validate_mode(
                    self.config or {},
                    config.SYSTEM_PROMPT_DIR / self.id,
                    mode_name,
                )
                if error:
                    self._emit_user_message(
                        task, emit, f"【模式切换失败】{error}", final=True, agent_id=self.id
                    )
                    return
                self.set_default_mode(mode_name)
                self.refresh_system_prompt()
                chat_log(
                    f"[{self.user_id}] {self.id} 模式切换 -> {self.current_mode()}"
                    f"（模型 {self.effective_config().get('model_profile', '')}）"
                )

            # 伪指令告警：`→tool: …` 不是协议，本轮会被当普通文本处理。
            # 静默是这类 bug 最糟的部分（模型以为转交了、实际什么都没发生），
            # 所以这里显式记一笔，便于线上发现复发。
            if has_misformatted_directive(raw_model_text):
                debug_log(
                    f"[{self.user_id}] [protocol] {self.id} 输出了非规范的箭头写法"
                    "（→agent: …）：它不是指令、不会被执行，已按普通文本处理；"
                    "正确写法是 对话:agent_id|内容。"
                )

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
                task.push_context(self, "【已发送给用户】\n询问：" + question, kind="question")
                task.set_temp_dialog_output("")
                task.status = "suspended"
                AgentRuntime.suspend_task(task, emit, question, agent_id=self.id)
                return

            elif result["timer_task"]:
                timer = result["timer_task"]
                client = TimerTaskClient()
                task_type = timer.get("task_type", "submit_task")

                if task_type == "invalid":
                    # 指令看着像定时任务但字段解析不出来：必须回一句用法，
                    # 否则用户只会看到"没反应"（模型也拿不到纠正机会）。
                    reply = (
                        "定时任务指令格式不对。正确写法：\n"
                        "  定时任务:submit_task|智能体id|内容|时间|重复|执行时段\n"
                        "  定时任务:send_message|智能体id|内容|时间|重复|执行时段\n"
                        "  定时任务:query|用户id\n"
                        "  定时任务:delete|任务id\n"
                        "（第 6 参「执行时段」可省略；写法如 09:00-22:00，"
                        "到点若不在时段内则本轮跳过不执行，等下一次触发）"
                    )
                    task.set_temp_dialog_output(reply)

                elif task_type == "delete":
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
                    # 统一 qq 渠道：定时任务不再写 `qqofficial_<uid>`，一律 `qq_<uid>`；
                    # 否则同一个人的记忆会被劈成 main_qq_* 与 main_qqofficial_* 两个 session。
                    timer_channel = (
                        "qq"
                        if str(task.channel or "").strip().lower()
                        in {"qq", "qqofficial", "qq_llbot", "qq-llbot", "llbot"}
                        else str(task.channel or "").strip()
                    )
                    timer_session_id = f"{timer_channel}_{task.user.id}"
                    resp = client.create_timer_task(
                        user_id=task.user.id,
                        session_id=timer_session_id,
                        channel_id=timer_channel,
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
                # 压帧内容与工具帧对齐：**既要有指令、也要有结果**。
                # 之前这里 push_context(self, "") → 弹栈后模型只看到【收到返回】，
                # 完全不知道自己下的是什么定时任务指令（无法据此继续决策）。
                raw_command = str(timer.get("raw", "") or "").strip()
                if raw_command:
                    task.tool_log.append("定时任务指令:" + raw_command)
                task.tool_log.append("定时任务结果:" + str(reply))
                task.push_context(
                    self,
                    "【本轮已执行的工具】\n" + "\n".join(task.tool_log),
                    kind="timer",
                )
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

        # 派发即入账（任务级 tool_log）：与 main_memory 的「已转交」同点同条件。
        # 放在这里而不是 syntax_parser，是为了让"这一轮被派了什么活"**无条件**留下 ——
        # 否则对方若不调工具直接回话，日志里就只有回复、没有请求，模型读不出上下文。
        # 智能体间对话要标注来源与时间：`[MM-DD HH:MM] <caller_id>:` 前缀。
        # 这样接收方（以及它的记忆、tool_log 的 `收到请求:`）都能看出这条是哪个智能体、
        # 什么时候说的。时间格式与 history_time_stamp 一致。
        dispatch_text = content
        raw_content = str(content or "")
        if raw_content and not _HAS_SPEAKER_PREFIX_RE.match(raw_content):
            stamp = time.strftime("%m-%d %H:%M")
            dispatch_text = f"[{stamp}] <{self.id}>:" + raw_content

        record_dispatch(task, target_agent_id, dispatch_text, self.id)
        task.push_context(
            self,
            "【已发送给" + target_agent_id + "】\n" + dispatch_text,
            kind="dialog",
            request=dispatch_text,
        )
        # `request` 会被 begin_work_episode() 清空，单独留一份派发原文给"被调用者记忆"用
        if task.agent_context:
            task.agent_context[-1]["dispatch"] = dispatch_text
        chat_log(f"[{self.user_id}] <{self.session_id}>:{self.id}->{target_agent_id}\n{dispatch_text}")
        debug_log(f"[{self.user_id}] [agent_call] <{self.session_id}>:{self.id}->{target_agent_id}")
        task.target = AgentRuntime.get_agent(target_agent_id, self.session_id, task.user.id)
        task.caller = self
        task.set_temp_dialog_input(dispatch_text)
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

            if event_type == "model_image":
                # image-view 的回填：图片不是发给用户的，而是**给模型看的**。
                # 这是唯一"允许自动切视觉模型"的显式路径（工具调用 = 模型主动要看图），
                # 所以打一次性标记：只让紧接着的那次模型调用走视觉模型，
                # 之后回到文本模型（见 send() 的视觉路由）。
                artifact = event.get("artifact") or {}
                viewed_url = artifact.get("asset_url", "")
                if viewed_url and viewed_url not in task.images:
                    task.images.append(viewed_url)
                if viewed_url:
                    task.metadata["_vision_explicit"] = "1"
                    debug_log(
                        f"[{task.user.id}] image-view 回填图片（本轮下一次调用走视觉模型）: "
                        f"{artifact.get('local_path', '')}"
                    )
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

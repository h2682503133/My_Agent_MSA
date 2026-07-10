import json
from collections import OrderedDict
from pathlib import Path
from typing import Callable

from app import config
from app.agent_config import load_agent_config
from app.context_client import ContextClient
from app.events import TaskEventDTO, DeliveryTarget, new_event_id
from app.logger import chat_log, debug_log
from app.model_proxy_client import ModelProxyClient
from app.response_parser import parse_model_response
from app.syntax_parser import parse_syntax
from app.tool_runtime_client import ToolRuntimeClient
from app.timer_task_client import TimerTaskClient


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
    - OpenViking 由 openviking-context-service 提供
    - 模型调用由 model-proxy-service 提供
    - 工具执行由 tool-runtime-service 提供
    - 用户消息发送由 TaskEvent stream 返回 scheduler
    """

    MAX_INSTANCES = 20
    _agent_instances: OrderedDict[str, "AgentRuntime"] = OrderedDict()
    default_agent: dict[str, str] = {}

    context_client = ContextClient()
    model_client = ModelProxyClient()
    tool_client = ToolRuntimeClient()

    def __init__(self, agent_id: str, session_id: str, user_id: str = "default"):
        self.id = agent_id
        self.session_id = session_id
        self.user_id = user_id or "default"
        self.config = {}
        self.system_prompt: list[dict[str, str]] = []
        self.load_config()
        self.build_system_prompt()

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
            },
        ))

        if final:
            task.status = "completed"

        return final_reply

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
            debug_log(f"[fallback_raw_model_output] {exc}")

        return cls._emit_user_message(task, emit, fallback_text, final=True)

    @classmethod
    def get_agent(cls, agent_id: str, session_id: str, user_id: str = "default") -> "AgentRuntime":
        user_id = user_id or "default"
        key = f"{user_id}_{session_id}_{agent_id}"
        if key in cls._agent_instances:
            cls._agent_instances.move_to_end(key)
            return cls._agent_instances[key]

        if len(cls._agent_instances) >= cls.MAX_INSTANCES:
            oldest_key = next(iter(cls._agent_instances))
            del cls._agent_instances[oldest_key]
            debug_log(f"[实例上限] 删除最久未使用: {oldest_key}")

        agent = cls(agent_id, session_id, user_id)
        cls._agent_instances[key] = agent
        debug_log(f"{session_id} 新建智能体: {agent_id}")
        return agent

    @classmethod
    def first_call(cls, task):
        agent_id = task.agent_id or cls.default_agent.get(task.user.session_id, "main")
        cls.default_agent[task.user.session_id] = agent_id
        target = cls.get_agent(agent_id, task.user.session_id, task.user.id)
        task.target = target
        chat_log(f"{task.user.session_id}->{target.id}\n{task.content}")
        debug_log(f"[user_chat]{task.user.session_id}->{target.id}")

    @classmethod
    def process_task(cls, task, emit: Callable[[TaskEventDTO], None]) -> str:
        debug_log(f"{task.user.id} 的任务正被 orchestrator 处理")
        final_reply = ""

        emit(cls.build_event(task, "task_started"))

        if len(task.agent_context) == 1:
            try:
                cls.first_call(task)
                task.default_agent = task.target
                task.default_agent.send(task, emit)
            except Exception as exc:
                raw_output = task.consume_temp_dialog_output() or task.send_text or ""
                return cls._emit_raw_model_fallback(task, emit, str(raw_output), exc)

        steps = 0
        while len(task.agent_context) > 0 and task.status == "running":
            steps += 1
            if steps > config.MAX_AGENT_STEPS:
                task.status = "failed"
                final_reply = "任务执行步数过多，已中止。"
                emit(cls.build_event(task, "task_failed", error=final_reply, text=final_reply))
                return final_reply

            debug_log(f"弹回复栈，当前栈长 {len(task.agent_context)}")
            context = task.pop_context()
            task.target = context["from"]
            request = context["input"]
            output = task.consume_temp_dialog_output() or "因不知名原因输出已丢失"

            if cls._is_user_object(task.target, task):
                final_reply = cls._emit_user_message(task, emit, output, final=True)
                break

            caller_id = getattr(task.caller, "id", "unknown")
            target_id = getattr(task.target, "id", "unknown")
            result = [target_id, request, caller_id, output]

            try:
                task.set_temp_dialog_input(result)
                task.target.send(task, emit)
            except Exception as exc:
                final_reply = cls._emit_raw_model_fallback(task, emit, str(output), exc)
                break

            final_reply = str(output)

        if task.status == "running":
            task.status = "completed"

        if not final_reply and task.send_text:
            final_reply = task.send_text

        if final_reply:
            try:
                default_agent = getattr(task, "default_agent", None)
                default_agent_id = getattr(default_agent, "id", "main")
                default_agent_config = getattr(default_agent, "config", {}) or {}
                cls.context_client.append_turn(
                    user_id=task.user.id,
                    session_id=task.user.session_id,
                    task_id=task.task_id,
                    user_message=task.content,
                    assistant_message=final_reply,
                    agent_id=default_agent_id,
                    tool_summaries=task.tool_log,
                    commit_limit=int(default_agent_config.get("commit_limit", 0) or 0),
                )
            except Exception as exc:
                debug_log(f"append_turn failed: {exc}")

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
        if not config.AGENT_CONFIG_PATH.exists():
            raise FileNotFoundError(f"未找到智能体配置：{config.AGENT_CONFIG_PATH}")
        self.config = load_agent_config(
            path=config.AGENT_CONFIG_PATH,
            user_id=self.user_id,
            agent_id=self.id,
        )

    def build_system_prompt(self):
        prompt_dir = config.SYSTEM_PROMPT_DIR / self.id
        global_setting = config.SYSTEM_PROMPT_DIR / "GLOBAL_SETTING.md"

        system_messages = []

        if global_setting.exists():
            content = global_setting.read_text(encoding="utf-8").strip()
            if content:
                system_messages.append({"role": "system", "content": content})

        for filename in self.config.get("files", []):
            file_path = prompt_dir / filename
            if not file_path.exists():
                continue
            content = file_path.read_text(encoding="utf-8").strip()
            if content:
                system_messages.append({"role": "system", "content": content})

        self.system_prompt = system_messages

    def send(self, task, emit: Callable[[TaskEventDTO], None]):
        content = task.consume_temp_dialog_input()

        if not isinstance(content, str):
            if content is not None:
                if content[0] == content[2]:
                    task.tool_log.append("结果" + str(content[3]))
                    content = f"\n{content[1]}，\n结果{content[3]}"
                else:
                    content = f"{content[0]}的请求：\n{content[1]},\n收到来自{content[2]}的回复：\n{content[3]}"
                    task.main_log.append(content)
                    if content[0] == "main":
                        task.main_memory.append(content)
            else:
                content = "当你看到这条消息时，意味着出现某些问题导致输入为空了"

        current_input_messages = [
            {"role": "system", "content": "以下为本次单轮对话内容"},
            {"role": "user", "content": f"<{getattr(task.caller, 'id', 'user')}>" + content},
        ]

        task.set_temp_dialog_input(content)
        chat_log(f"{self.id}收到:\n{content}")

        long_context_message = [
            {"role": "system", "content": "以下是你和用户的历史对话记录，请根据上下文继续回答"}
        ] + self.context_client.search_context(
            user_id=task.user.id,
            session_id=task.user.session_id,
            agent_id=self.id,
            query=content,
            max_messages=int(self.config.get("max_messages", 6) or 6),
            max_tokens=int(self.config.get("context_max_tokens", 3000) or 3000),
            commit_limit=int(self.config.get("commit_limit", 0) or 0),
        )

        system_prompt_messages = self.system_prompt

        task_memory_messages = []
        if self.id == "main":
            task_memory = "以下是你在本次任务中的记忆:"
            for item in task.main_memory:
                task_memory += "\n" + item
            task_memory_messages = [{"role": "system", "content": task_memory}]

        user_input_messages = [
            {"role": "system", "content": "以下为本次请求对话，请着重于下面部分\n下面是该任务用户原始请求"},
            {"role": "user", "content": f"<{task.user.id}>" + task.content},
        ]

        messages = (
            long_context_message
            + task_memory_messages
            + system_prompt_messages
            + user_input_messages
            + current_input_messages
        )

        model_profile = self.config.get("model_profile") or self.config.get("model") or self.id
        params = {
            "temperature": self.config.get("temperature", 1),
            "max_tokens": self.config.get("max_tokens", 2048),
            "stream": False,
        }

        try:
            model_response = self.model_client.chat_completion(
                task_id=task.task_id,
                agent_id=self.id,
                model_profile=model_profile,
                messages=messages,
                params=params,
            )
        except Exception as exc:
            error_text = f"【模型请求失败】{exc}"
            self._emit_user_message(task, emit, error_text, final=True)
            return

        raw_model_text = self._extract_raw_model_text(model_response)

        try:
            parsed = parse_model_response(self.id, model_response)
            raw_model_text = parsed.text or raw_model_text
            task.set_temp_dialog_output(parsed.text)

            parse_syntax(self, task)
            result = task.consume_temp_dialog_output()

            if result.get("switch_call") and result["agent_call"]:
                debug_log(
                    f"[switch_route] {self.id} -> {result['agent_call']['target_id']} "
                    f"content={result['agent_call']['content']}"
                )
                chat_log(
                    f"{self.id} 切换并转交:\n "
                    f"{result['agent_call']['target_id']}|{result['agent_call']['content']}"
                )
            else:
                chat_log(f"{self.id} 回复:\n {result['final_reply']}")

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
                    self._emit_user_message(task, emit, content_for_target, final=True)
                    return

                task.set_temp_dialog_input(content_for_target)
                self.call_agent(target_agent_id, task, emit)

            elif result["question"]:
                question = (result["question"] or "").strip() or "请补充必要信息后我再继续。"
                # 微服务版每个 ExecuteTask 都是独立请求，TaskRuntime 的 agent_context
                # 不会跨请求持久化；不能再把本轮任务置为 pause 后等待恢复。
                # 这里把 `询问:xxx` 作为本轮最终回复发给用户，下一轮用户回答会通过
                # context-service 带上这轮问题继续处理。
                self._emit_user_message(task, emit, question, final=True)
                return

            elif result["timer_task"]:
                timer = result["timer_task"]
                client = TimerTaskClient()
                resp = client.create_timer_task(
                    user_id=task.user.id,
                    session_id=task.user.session_id,
                    channel_id=task.channel,
                    trigger_timestamp=timer.get("trigger_timestamp", 0),
                    content=timer.get("content", ""),
                    task_type=timer.get("task_type", "submit_task"),
                    agent_id=timer.get("agent_id", "") or getattr(self, "id", ""),
                )
                if resp["ok"]:
                    reply = f"定时任务已创建：{resp['message']}"
                else:
                    reply = f"定时任务创建失败：{resp['message']}"
                task.set_temp_dialog_output(reply)
                emit(self.build_event(
                    task,
                    "assistant_intermediate",
                    text=reply,
                    metadata={"visible_to_user": "true", "final": "false"},
                ))

            else:
                final_reply = result["final_reply"]
                task.set_temp_dialog_output(final_reply)

        except Exception as exc:
            self._emit_raw_model_fallback(task, emit, raw_model_text, exc)
            return

    def call_agent(self, target_agent_id: str, task, emit: Callable[[TaskEventDTO], None]):
        content = task.consume_temp_dialog_input()

        if self._is_user_agent_id(target_agent_id, task):
            self._emit_user_message(task, emit, content, final=True)
            return

        task.push_context(self, content)
        chat_log(f"<{self.session_id}>:{self.id}->{target_agent_id}\n{content}")
        debug_log(f"[agent_call] <{self.session_id}>:{self.id}->{target_agent_id}")
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
        debug_log(f"[工具执行] {self.id} → {tool_name} {args}")

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
        result = self.tool_client.execute_tool(
            task_id=task.task_id,
            tool_name=tool_name,
            args=args,
            user_id=task.user.id,
            session_id=task.user.session_id,
        )

        if result["ok"]:
            output = result["output"]
            for artifact in result.get("artifacts", []):
                if artifact.get("asset_url"):
                    task.send_images.append(artifact["asset_url"])
        else:
            output = f"工具执行失败：{result['error']}"

        task.set_temp_dialog_output(output)
        chat_log(f"{self.id} 执行工具 {tool_name}:\n结果: {output}")
        debug_log(f"[工具结果] {self.id} {output}")

"""
从原 core/Agent/syntax_parser.py 拆出并适配。

保留解析协议：
- 对话:target|content
- 工具调用:tool|arg1|arg2
- 工具调用:shell|raw linux command
- 询问:xxx
- 切换:xxx
- 切换到xxx智能体
- 定时任务:类型|时间|内容
"""

from datetime import datetime
import re


_COMMAND_NAMES = ("对话", "工具调用", "切换", "定时任务")
_COMMAND_LINE_RE = re.compile(
    r"^\s*(?:[-*•]\s*)?(?:" + "|".join(map(re.escape, _COMMAND_NAMES)) + r")\s*:"
)
_SHELL_TOOL_NAMES = {"shell", "run-shell", "command"}
# 内容类工具：最后一个参数可能包含 | 和换行，需用 split("|", 2) 保留
_CONTENT_TOOLS = {"file-write", "codex"}
_PRIORITY_SHELL_RE = re.compile(r"^\s*(?:[-*•]\s*)?工具调用\s*:\s*shell\s*\|\s*(.*)$")
# 用于在文本任意位置（非行首）匹配指令关键字，处理模型先说一段话再输出指令的场景
_INLINE_COMMAND_RE = re.compile(
    r"(?:对话|工具调用|切换|定时任务)\s*:"
)
_INLINE_SHELL_RE = re.compile(r"工具调用\s*:\s*shell\s*\|\s*")


def clean_ai_thinking(text: str) -> str:
    """彻底清洗 AI 思考内容，防止语法解析误触发"""
    if not text or not isinstance(text, str):
        return ""
    if "</think>" in text:
        text = text.split("</think>")[-1]
    return text.strip()


def _normalize_text(text: str) -> str:
    return (text or "").replace("：", ":").strip()


def _is_command_line(line: str) -> bool:
    return bool(_COMMAND_LINE_RE.match(line or ""))


def _find_command_inline(full_text: str, command_name: str) -> str | None:
    """
    行首匹配失败时的 fallback：在文本任意位置搜索指令关键字。

    处理部分模型喜欢先说一段话再输出指令的场景，例如：
        好的，我来帮您处理这个问题。对话:target|content
    """
    pattern = re.compile(rf"{re.escape(command_name)}\s*:\s*")
    match = pattern.search(full_text)
    if not match:
        return None

    start = match.end()
    remaining = full_text[start:]

    # 截取到下一个指令关键字或文本末尾
    next_match = _INLINE_COMMAND_RE.search(remaining)
    if next_match:
        value = remaining[:next_match.start()].strip()
    else:
        value = remaining.strip()

    return value or None


def _find_command_block(full_text: str, command_name: str, allow_multiline: bool = False) -> str | None:
    """
    查找行首协议指令。

    注意：`询问:` 不使用本函数；它按兼容原逻辑的方式在最后判断，
    只要文本任意位置出现 `询问:`，就取其后的全部内容作为用户可见问题。
    """
    pattern = re.compile(rf"^\s*(?:[-*•]\s*)?{re.escape(command_name)}\s*:\s*(.*)$")
    lines = full_text.splitlines() or [full_text]

    for index, line in enumerate(lines):
        match = pattern.match(line)
        if not match:
            continue

        value_lines = [match.group(1).strip()]
        if allow_multiline:
            for extra_line in lines[index + 1:]:
                if _is_command_line(extra_line):
                    break
                value_lines.append(extra_line.rstrip())

        value = "\n".join(value_lines).strip()
        return value or None

    # 行首匹配失败，尝试内联 fallback
    return _find_command_inline(full_text, command_name)


def _find_question_tail(full_text: str) -> str | None:
    """
    兼容 `询问:xxx` 的原始宽松写法。

    和其他协议不同，`询问:` 允许出现在文本任意位置；一旦最后轮到
    询问逻辑，就把 `询问:` 之后的全部内容直接作为要发给用户的问题。
    """
    marker = "询问:"
    index = full_text.find(marker)
    if index < 0:
        return None
    return full_text[index + len(marker):].strip()


def _parse_tool_call(tool_line: str) -> dict | None:
    """
    解析工具调用。

    普通工具继续使用 `|` 分隔参数：
        工具调用:file-read|a.txt

    shell 类工具只按第一个 `|` 切分，后面的内容作为原始 shell command
    完整保留，避免 `ps aux | grep python` 里的管道被协议层吞掉。
    """
    if not tool_line:
        return None

    first_part = tool_line.split("|", 1)[0].strip()
    if not first_part:
        return None

    if first_part in _SHELL_TOOL_NAMES:
        command = ""
        if "|" in tool_line:
            _, command = tool_line.split("|", 1)
        command = command.strip()
        if not command:
            return None
        return {
            "tool": first_part,
            "args": [command],
            "kwargs": {"command": command},
        }

    if first_part in _CONTENT_TOOLS:
        # 内容工具的最后一个参数可能包含 | 和换行，
        # 用 split("|", 2) 保留第二个 | 之后的所有内容作为最后一个参数
        parts = tool_line.split("|", 2)
        tool_name = parts[0].strip()
        args = [p.strip() for p in parts[1:] if p.strip()]
        if not tool_name:
            return None
        return {
            "tool": tool_name,
            "args": args,
            "kwargs": {},
        }

    parts = tool_line.split("|")
    tool_name = parts[0].strip()
    args = [p.strip() for p in parts[1:] if p.strip()]
    if not tool_name:
        return None
    return {
        "tool": tool_name,
        "args": args,
        "kwargs": {},
    }


def _find_priority_shell_call(full_text: str) -> tuple[str, dict] | tuple[None, None]:
    """
    最高优先级识别 `工具调用:shell|...`。

    shell 命令本身经常包含 `|`、`>`、`&&`、`;` 等 shell 语法，
    因此不能等普通工具协议解析。只要任何一行命中 `工具调用:shell|`，
    就把它视作本轮唯一工具调用，并保留其后的原始命令文本。

    如果 shell 命令写成多行，则会继续读取后续非协议行，直到遇到
    下一条 `对话:` / `工具调用:` / `切换:` / `定时任务:`。
    """
    lines = full_text.splitlines() or [full_text]

    for index, line in enumerate(lines):
        match = _PRIORITY_SHELL_RE.match(line)
        if not match:
            continue

        command_lines = [match.group(1).strip()]
        for extra_line in lines[index + 1:]:
            if _is_command_line(extra_line):
                break
            command_lines.append(extra_line.rstrip())

        command = "\n".join(command_lines).strip()
        if not command:
            return None, None

        tool_line = f"shell|{command}"
        return tool_line, {
            "tool": "shell",
            "args": [command],
            "kwargs": {"command": command},
        }

    # 行首匹配失败，尝试内联 fallback
    return _find_priority_shell_inline(full_text)


def _find_priority_shell_inline(full_text: str):
    """_find_priority_shell_call 的内联 fallback。处理 shell 命令出现在段落中间的场景。"""
    match = _INLINE_SHELL_RE.search(full_text)
    if not match:
        return None, None

    remaining = full_text[match.end():]
    next_match = _INLINE_COMMAND_RE.search(remaining)
    if next_match:
        command = remaining[:next_match.start()].strip()
    else:
        command = remaining.strip()

    if not command:
        return None, None

    tool_line = f"shell|{command}"
    return tool_line, {
        "tool": "shell",
        "args": [command],
        "kwargs": {"command": command},
    }


def to_timestamp(time_str: str) -> float:
    dt = datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S")
    return dt.timestamp()


def parse_syntax(agent, task):
    raw_text = task.consume_temp_dialog_output()
    raw_text = clean_ai_thinking(raw_text)
    full_text = _normalize_text(raw_text)

    reply = full_text
    agent_call = None
    tool_call = None
    question = None
    timer_task = None
    switch_call = None

    # 最高优先级：shell 原始命令。
    # 只要命中 `工具调用:shell|...`，就不要再解析其它协议，避免 shell
    # 管道/重定向/多行命令被普通工具分隔逻辑或对话/切换逻辑干扰。
    priority_tool_line, priority_tool_call = _find_priority_shell_call(full_text)
    if priority_tool_call:
        task.tool_log.append("调用工具:" + priority_tool_line)
        stack_parts = []
        if task.tool_log:
            stack_parts.append("【本轮已执行的工具】\n" + "\n".join(task.tool_log))
        if task.last_dialog_content:
            task.tool_log.append("收到请求:" + task.last_dialog_content)
            task.last_dialog_content = ""
        stack_content = "\n".join(stack_parts) if stack_parts else ""
        task.push_context(agent, stack_content)

        task.set_temp_dialog_output({
            "final_reply": reply,
            "reply": full_text,
            "tool_call": priority_tool_call,
            "agent_call": None,
            "question": None,
            "timer_task": None,
            "switch_call": None,
        })
        return

    # 保持和原项目接近的优先级：先解析智能体调用。
    agent_line = _find_command_block(full_text, "对话", allow_multiline=True)
    if agent_line and "|" in agent_line:
        target_id, content = agent_line.split("|", 1)
        target_id = target_id.strip()
        content = content.strip()
        if target_id and content:
            agent_call = {
                "target_id": target_id,
                "content": content,
            }

    # 然后解析工具调用。
    tool_line = _find_command_block(full_text, "工具调用", allow_multiline=True)
    if tool_line:
        task.tool_log.append("调用工具:" + tool_line)
        stack_parts = []
        if task.tool_log:
            stack_parts.append("【本轮已执行的工具】\n" + "\n".join(task.tool_log))
        if task.last_dialog_content:
            task.tool_log.append("收到请求:" + task.last_dialog_content)
            task.last_dialog_content = ""
        stack_content = "\n".join(stack_parts) if stack_parts else ""
        task.push_context(agent, stack_content)

        tool_call = _parse_tool_call(tool_line)

    switch_target = None
    pure_switch = False

    # 切换智能体：更新默认智能体；若本轮只输出切换指令，则转成一次真实的 agent_call。
    switch_line = _find_command_block(full_text, "切换", allow_multiline=False)
    if switch_line:
        agent_id = switch_line.strip()
        if agent_id:
            switch_target = agent_id
            # 允许 preamble 文本：只要没有其他指令关键字，视为纯切换
            has_other = bool(
                _find_command_block(full_text, "对话")
                or _find_command_block(full_text, "工具调用")
                or _find_command_block(full_text, "定时任务")
            )
            pure_switch = not has_other
            switch_call = {"target_id": agent_id, "pure": pure_switch}
            agent.set_default_agent(agent_id)

    # 切换到xxx智能体：也支持内联在段落中
    switch2_re = re.compile(r"切换到(\w+)智能体")
    match_switch2 = switch2_re.search(full_text)
    if match_switch2:
        agent_id = match_switch2.group(1).strip()
        if agent_id and not switch_target:
            switch_target = agent_id
            has_other = bool(
                _find_command_block(full_text, "对话")
                or _find_command_block(full_text, "工具调用")
                or _find_command_block(full_text, "定时任务")
            )
            pure_switch = not has_other
            switch_call = {"target_id": agent_id, "pure": pure_switch}
            agent.set_default_agent(agent_id)

    if (
        switch_target
        and pure_switch
        and not agent_call
        and not tool_call
        and switch_target != getattr(agent, "id", "")
    ):
        # 纯切换不转交目标智能体，保持原始回复内容（reply 仍为 full_text）
        pass

    # 定时任务在询问之前判断，避免同时出现时被询问分支抢走。
    timer_line = _find_command_block(full_text, "定时任务", allow_multiline=False)
    if timer_line:
        # 统一格式: 定时任务:任务类别|智能体id|任务内容|任务时间
        match_timer = re.match(r"([^|]+)\|([^|]+)\|([^|]+)(?:\|(.+))?", timer_line)
        if match_timer:
            task_type = match_timer.group(1).strip()
            agent_id = match_timer.group(2).strip()
            content = match_timer.group(3).strip()
            time_str = match_timer.group(4).strip() if match_timer.group(4) else ""

            if task_type in ("delete", "query"):
                # 定时任务:delete|agent_id|task_id|
                # 定时任务:query|agent_id|user_id|
                timer_task = {
                    "task_type": task_type,
                    "content": content,
                    "agent_id": agent_id,
                    "time_str": "",
                    "trigger_timestamp": 0.0,
                }
            else:
                # submit_task / send_message: 定时任务:类型|agent_id|内容|时间
                time_str = time_str or "2026-01-31 00:00:00"
                try:
                    trigger_ts = to_timestamp(time_str)
                    timer_task = {
                        "task_type": task_type,
                        "time_str": time_str,
                        "trigger_timestamp": trigger_ts,
                        "content": content,
                        "agent_id": agent_id,
                    }
                except Exception:
                    pass

    # 最后才判断询问：只有没有工具、智能体调用、定时任务时，才把
    # `询问:` 之后的全部内容作为最终用户可见问题。
    question_tail = _find_question_tail(full_text)
    if question_tail is not None and not tool_call and not agent_call and not timer_task:
        question = question_tail
        reply = question

    task.set_temp_dialog_output({
        "final_reply": reply,
        "reply": full_text,
        "tool_call": tool_call,
        "agent_call": agent_call,
        "question": question,
        "timer_task": timer_task,
        "switch_call": switch_call,
    })
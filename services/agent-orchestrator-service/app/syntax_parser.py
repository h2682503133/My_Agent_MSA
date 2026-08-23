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

import re


_COMMAND_NAMES = ("对话", "工具调用", "切换", "定时任务")
_COMMAND_LINE_RE = re.compile(
    r"^\s*(?:[-*•`]\s*)?(?:" + "|".join(map(re.escape, _COMMAND_NAMES)) + r")\s*:"
)
_SHELL_TOOL_NAMES = {"shell", "run-shell", "command"}
# 内容类工具：最后一个参数可能包含 | 和换行，需用 split("|", 2) 保留
_CONTENT_TOOLS = {"file-write", "codex"}
# 位置敏感工具：保留空槽，保证 fetch 参数不错位
_POSITIONAL_TOOLS = {"fetch"}
_PRIORITY_SHELL_RE = re.compile(r"^\s*(?:[-*•`]\s*)?工具调用\s*:\s*shell\s*\|\s*(.*)$")
# 用于在文本任意位置（非行首）匹配指令关键字，处理模型先说一段话再输出指令的场景
_INLINE_COMMAND_RE = re.compile(
    r"(?:对话|工具调用|切换|定时任务)\s*:"
)
_INLINE_SHELL_RE = re.compile(r"工具调用\s*:\s*shell\s*\|\s*")
# 询问指令：必须是独立关键字，前面不能是汉字/字母/数字，
# 避免「请询问:」「我询问:」「想询问:」这类自然语言被误判为询问指令。
_QUESTION_CMD_RE = re.compile(r"(?<![一-龥A-Za-z0-9])询问\s*:\s*(.*)$", re.S)
# 任意指令关键字的起始位置（约束与各 _find_* 一致），
# 用于提取指令前的说明文本，供中间过程转发给用户。
_INSTRUCTION_START_RE = re.compile(
    r"(?:对话|工具调用|切换|定时任务)\s*:"
    r"|(?<![一-龥A-Za-z0-9])询问\s*:"
    r"|切换到\w+智能体"
)


def _clean_shell_command(command: str) -> str:
    """去掉模型用反引号包裹 shell 指令时残留的尾部反引号。

    仅在反引号数量为奇数时清理（反引号包裹场景），
    避免误伤 shell 命令替换（如 echo `date`）中的合法反引号。
    """
    command = (command or "").strip()
    if command.count("`") % 2 == 1:
        command = command.rstrip("`").rstrip()
    return command


def clean_ai_thinking(text: str) -> str:
    """彻底清洗 AI 思考内容，防止语法解析误触发"""
    if not text or not isinstance(text, str):
        return ""
    if "</think>" in text:
        text = text.split("</think>")[-1]
    return text.strip()


def _normalize_text(text: str) -> str:
    text = (text or "").replace("：", ":").strip()
    # 兼容模型把协议关键字顺序写反：调用工具:xxx → 工具调用:xxx
    # （否则 _find_command_block 匹配不到，工具调用会被当成普通回复直接返回）
    text = text.replace("调用工具:", "工具调用:")
    return text


def extract_command_prefix(full_text: str) -> str:
    """
    提取第一条指令关键字之前的说明文本。

    例：好的，我先搜索一下。工具调用:web-search|关键词|10
    → 好的，我先搜索一下。
    指令在行首（或没有指令）时返回空串。
    """
    if not full_text:
        return ""
    match = _INSTRUCTION_START_RE.search(full_text)
    if not match:
        return ""
    prefix = full_text[: match.start()]
    # 指令若以列表符（- * •）起始，去掉前缀末尾的列表符
    prefix = re.sub(r"[-*•`]\s*$", "", prefix).strip()
    return prefix


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
    pattern = re.compile(rf"^\s*(?:[-*•`]\s*)?{re.escape(command_name)}\s*:\s*(.*)$")
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

    和其他协议不同，`询问:` 允许出现在段落中间（前面是标点/空白）；
    但「询问:」必须是独立指令关键字，前面不能是汉字/字母/数字，
    避免「请询问:」「我询问:」等自然语言被误判为询问指令。
    """
    match = _QUESTION_CMD_RE.search(full_text)
    if not match:
        return None
    return match.group(1).strip()


def _parse_tool_call(tool_line: str) -> dict | None:
    """
    解析工具调用。

    普通工具继续使用 `|` 分隔参数：
        工具调用:file-read|a.txt

    shell 类工具只按第一个 `|` 切分，后面的内容作为原始 shell command
    完整保留，避免 `ps aux | grep python` 里的管道被协议层吞掉。

    fetch 系列工具（fetch / fetch-outline）保留空槽，保证位置参数不错位：
        工具调用:fetch|url|GET||问题
        → args = ["url", "GET", "", "问题"]（query 落在第 5 个位置）
    """
    if not tool_line:
        return None

    # 兼容模型用反引号包裹指令/参数（如 工具调用:xxx|内容`），避免污染参数
    tool_line = tool_line.strip().strip("`")

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
        args = [p.strip().strip("`") for p in parts[1:] if p.strip().strip("`")]
        if not tool_name:
            return None
        return {
            "tool": tool_name,
            "args": args,
            "kwargs": {},
        }

    if first_part in _POSITIONAL_TOOLS:
        # 位置敏感工具：保留空槽，避免 fetch|url|GET||问题 的 query 错位
        parts = tool_line.split("|")
        tool_name = parts[0].strip()
        args = [p.strip().strip("`") for p in parts[1:]]
        if not tool_name:
            return None
        return {
            "tool": tool_name,
            "args": args,
            "kwargs": {},
        }

    parts = tool_line.split("|")
    tool_name = parts[0].strip()
    args = [p.strip().strip("`") for p in parts[1:] if p.strip().strip("`")]
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

        command = _clean_shell_command("\n".join(command_lines))
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
        command = _clean_shell_command(remaining[:next_match.start()])
    else:
        command = _clean_shell_command(remaining)

    if not command:
        return None, None

    tool_line = f"shell|{command}"
    return tool_line, {
        "tool": "shell",
        "args": [command],
        "kwargs": {"command": command},
    }


def parse_syntax(agent, task):
    raw_text = task.consume_temp_dialog_output()
    raw_text = clean_ai_thinking(raw_text)
    full_text = _normalize_text(raw_text)

    reply = full_text
    prefix = extract_command_prefix(full_text)
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
        # 「收到请求」在「调用工具」之前：先收到 main 等转交的要求，再执行工具
        if task.last_dialog_content:
            task.tool_log.append("收到请求:" + task.last_dialog_content)
            task.last_dialog_content = ""
        task.tool_log.append("调用工具:" + priority_tool_line)
        stack_parts = []
        if task.tool_log:
            stack_parts.append("【本轮已执行的工具】\n" + "\n".join(task.tool_log))
        stack_content = "\n".join(stack_parts) if stack_parts else ""
        task.push_context(agent, stack_content)

        task.set_temp_dialog_output({
            "final_reply": reply,
            "reply": full_text,
            "prefix": prefix,
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
        # 「收到请求」在「调用工具」之前：先收到 main 等转交的要求，再执行工具
        if task.last_dialog_content:
            task.tool_log.append("收到请求:" + task.last_dialog_content)
            task.last_dialog_content = ""
        task.tool_log.append("调用工具:" + tool_line)
        stack_parts = []
        if task.tool_log:
            stack_parts.append("【本轮已执行的工具】\n" + "\n".join(task.tool_log))
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
        # 统一格式: 定时任务:任务类别|智能体id|任务内容|开始时间|重复计划(可选,0=不重复)
        match_timer = re.match(r"([^|]+)\|([^|]+)\|([^|]+)(?:\|(.*))?", timer_line)
        if match_timer:
            task_type = match_timer.group(1).strip()
            agent_id = match_timer.group(2).strip()
            content = match_timer.group(3).strip()
            rest = match_timer.group(4) or ""
            rest_parts = rest.split("|")
            start_time = rest_parts[0].strip() if rest_parts else ""
            repeat_str = rest_parts[1].strip() if len(rest_parts) > 1 else ""

            if task_type in ("delete", "query"):
                # 定时任务:delete|agent_id|task_id|
                # 定时任务:query|agent_id|user_id|
                timer_task = {
                    "task_type": task_type,
                    "content": content,
                    "agent_id": agent_id,
                    "time_str": "",
                    "repeat_str": "",
                    "trigger_timestamp": 0.0,
                }
            else:
                # submit_task / send_message:
                #   第4参 开始时间：绝对 2026-01-31 10:00 / 当天 10:00 / 10点30分
                #     相对 5分钟后 / 明天10:00 / 区间随机 10:00-11:00 / 现在 / 空=立即
                #   第5参 重复计划（可选，0=不重复）：
                #     每30分钟 / 每2小时 / 每1小时30分钟 / 每5-10分钟
                timer_task = {
                    "task_type": task_type,
                    "time_str": start_time,
                    "repeat_str": repeat_str,
                    "trigger_timestamp": 0.0,
                    "content": content,
                    "agent_id": agent_id,
                }

    # 最后才判断询问：只有没有工具、智能体调用、定时任务时，才把
    # `询问:` 之后的全部内容作为最终用户可见问题。
    question_tail = _find_question_tail(full_text)
    if question_tail is not None and not tool_call and not agent_call and not timer_task:
        question = question_tail
        reply = question

    task.set_temp_dialog_output({
        "final_reply": reply,
        "reply": full_text,
        "prefix": prefix,
        "tool_call": tool_call,
        "agent_call": agent_call,
        "question": question,
        "timer_task": timer_task,
        "switch_call": switch_call,
    })

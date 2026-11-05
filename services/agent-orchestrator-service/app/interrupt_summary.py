"""步数上限中断时的"收尾摘要"（纯函数，便于单测）。

背景：`_continue_task` 里 `steps > MAX_AGENT_STEPS` 以前是**直接 return**：
不是只少一句解释 —— 它把整轮执行现场（agent_context 栈 / main_log / tool_log）全部丢掉，
既不写短期记忆、也不留工具经验，用户只能看到"任务执行步数过多，已中止。"，
下次还得从头再跑一遍。

现在改成：把两本账（主执行日志 main_log + 工具执行日志 tool_log）拼成一份受控长度的
transcript，交给 reader 模型压缩，产出"已完成 / 关键数据 / 未完成+下一步"，
作为**最终输出**发给用户，并写进会话历史供下一轮接着用。

本模块只做纯文本处理，不碰网络/存储：
- build_transcript()   两本账（对话指令/回复 + 工具执行）→ 受控长度 transcript
- reader_messages()    给 reader 的固定提示词
- render_final_text()  摘要 → 给用户的最终文本（带中断说明）
- fallback_summary()   reader 不可用时的兜底摘要（最后几条原始记录）
"""
from __future__ import annotations

DEFAULT_PER_ENTRY_CHARS = 400
DEFAULT_MAX_INPUT_CHARS = 12_000
DEFAULT_KEEP_TAIL_ENTRIES = 3

READER_SYSTEM_PROMPT = (
    "你是一个任务收尾器。下面是一个**因执行步数达到上限而被强制中止**的任务记录，"
    "包含两份日志：「主日志」记两类：`对话: 目标|内容`（派发指令）与 `收到回复: 目标|内容`（对方回复），「工具执行日志」记录工具调用与结果。\n\n"
    "请只输出以下三部分（不要复述过程、不要写客套话）：\n"
    "1) 已完成：已经做成的结论/产出/落盘文件路径（有就写具体路径）\n"
    "2) 关键数据：过程中拿到且值得保留的事实（ID、URL、数值、配置项、结论）\n"
    "3) 未完成：中断在哪一步，下一步该继续做什么（具体到动作）\n\n"
    "整段不超过 400 字；日志里没有的信息不要编。"
)


def _clean(value: object) -> str:
    return str(value if value is not None else "").strip()


def build_transcript(
    main_log: list[str] | None,
    tool_log: list[str] | None,
    *,
    per_entry_chars: int = DEFAULT_PER_ENTRY_CHARS,
    max_chars: int = DEFAULT_MAX_INPUT_CHARS,
) -> str:
    """把两份日志拼成 transcript：先主日志、后工具日志；每条截断；总长受控。

    - 超长时**保留头部**（任务早期通常定下了目标与关键结论）并标注截掉了多少；
    - 任何一条为空/None 都跳过，不产生 "None" 文本。
    """
    blocks: list[str] = []
    for title, entries in (("主日志（对话指令与回复）", main_log or []), ("工具执行日志", tool_log or [])):
        lines = []
        for raw in entries:
            text = _clean(raw)
            if not text:
                continue
            if per_entry_chars > 0 and len(text) > per_entry_chars:
                text = text[:per_entry_chars] + "…"
            lines.append(f"- {text}")
        if lines:
            blocks.append(f"## {title}\n" + "\n".join(lines))
    transcript = "\n\n".join(blocks)
    if max_chars > 0 and len(transcript) > max_chars:
        kept = transcript[:max_chars]
        transcript = f"{kept}\n…（日志过长，已截断 {len(transcript) - len(kept)} 字）"
    return transcript


def reader_messages(
    main_log: list[str] | None,
    tool_log: list[str] | None,
    *,
    per_entry_chars: int = DEFAULT_PER_ENTRY_CHARS,
    max_chars: int = DEFAULT_MAX_INPUT_CHARS,
) -> list[dict[str, str]]:
    """给 reader 的 messages；transcript 为空时返回空列表（调用方跳过总结）。"""
    transcript = build_transcript(
        main_log, tool_log, per_entry_chars=per_entry_chars, max_chars=max_chars
    )
    if not transcript:
        return []
    return [
        {"role": "system", "content": READER_SYSTEM_PROMPT},
        {"role": "user", "content": transcript},
    ]


def render_final_text(summary: str, *, steps: int, limit: int) -> str:
    """给用户的最终文本：中断说明 + 摘要。summary 为空时退回原文案。"""
    head = f"任务执行步数过多（{steps}/{limit} 步），已中止。"
    body = _clean(summary)
    if not body:
        return head
    return f"{head}\n以下是这轮已经拿到的进展（摘要）：\n{body}"


def fallback_summary(
    main_log: list[str] | None,
    tool_log: list[str] | None,
    *,
    keep: int = DEFAULT_KEEP_TAIL_ENTRIES,
) -> str:
    """reader 不可用时的兜底：最后几条原始记录，不做压缩，但保证不丢现场。"""
    picked: list[str] = []
    for entries in (tool_log or [], main_log or []):
        tail = [_clean(e) for e in entries if _clean(e)][-keep:]
        for item in tail:
            picked.append(f"- {item[:200]}")
    if not picked:
        return ""
    return "（reader 不可用，以下为中断前的最后几条原始记录）\n" + "\n".join(picked)

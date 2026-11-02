"""openviking-context-service 不可用时的本地降级上下文（两级）。

① 窗口滚动：保留最近 max_messages 条消息（默认 6，旧行为是固定 4 个回合）；
② 轮数压缩：未压缩回合数达到 commit_limit（与 OpenViking 同一语义，
   来自 agent_list.json / 调用方）时，把「滚出窗口的回合 + 既有长期摘要」
   打包给 reader 模型（默认 default-reader）压成新的长期摘要。

与 OpenViking 的接口保持一致：上层只调 `search()` / `append()`，切档不改业务代码。

存储：<PROCESS_DIR 同级>/local_context/<user>__<session>__<agent>.json
  {
    "turns": [...],                  # 未压缩的回合（最多保留 commit_limit + max_messages 条）
    "summary": "长期摘要",            # 压缩产物（注入时以【长期记忆】前缀出现）
    "committed": 12,                 # 已压缩的回合数（仅用于统计/诊断）
    "compressed_at": "..."
  }
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path

from app import config
from app.logger import debug_log

# 窗口默认条数（旧行为是 4 个回合，这里改成"条"以对齐 OpenViking 的 max_messages）
DEFAULT_MAX_MESSAGES = 6
# 旧常量保留，兼容历史调用/测试
MAX_TURNS = DEFAULT_MAX_MESSAGES

# 压缩时每个回合喂给模型的最大字符数（避免一次塞爆）
TURN_CHARS = 300
# 既有摘要保留上限
SUMMARY_KEEP_CHARS = 1200
# 压缩后摘要上限
SUMMARY_MAX_CHARS = 800


def _store_dir() -> Path:
    base = Path(getattr(config, "PROCESS_DIR", "/app/config/process"))
    d = base.parent / "local_context"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _safe(part: str) -> str:
    """清洗标识符，防止路径穿越；保留字母数字与常见符号。"""
    cleaned = re.sub(r"[^0-9A-Za-z_\-.@]", "_", part or "default")
    return cleaned[:64] or "default"


def _file_for(user_id: str, session_id: str, agent_id: str) -> Path:
    return _store_dir() / f"{_safe(user_id)}__{_safe(session_id)}__{_safe(agent_id)}.json"


def _load(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as exc:  # noqa: BLE001
        debug_log(f"[local_context] 读取 {path.name} 失败（按空处理）: {exc}")
        return {}


def _max_messages() -> int:
    try:
        return max(1, int(os.getenv("LOCAL_CONTEXT_MAX_MESSAGES", DEFAULT_MAX_MESSAGES)))
    except ValueError:
        return DEFAULT_MAX_MESSAGES


def _window_turns(max_messages: int) -> int:
    """窗口按「消息条数」配置，存储按「回合」保存（1 回合 = 用户 + 助手两条）。"""
    return max(1, (int(max_messages) + 1) // 2)


def _reader_profile() -> str:
    return os.getenv("LOCAL_MEMORY_PROFILE", "default-reader").strip() or "default-reader"


def _summarize(prompt: str) -> str:
    """调用 reader 模型压缩；失败抛异常，由上层的调用点决定是否保留旧摘要。

    单独抽成函数，便于离线测试替换（不联网）。
    """
    from app.model_proxy_client import model_proxy_client

    messages = [
        {"role": "system", "content": "你是对话记忆压缩器。只输出压缩后的记忆本身，不要解释、不要开场白。"},
        {"role": "user", "content": prompt},
    ]
    response = model_proxy_client.chat_completion(
        task_id="local-memory-compress",
        agent_id="reader",
        model_profile=_reader_profile(),
        messages=messages,
        params={"temperature": 0.2, "max_tokens": SUMMARY_MAX_CHARS},
    )
    text = ""
    if isinstance(response, dict):
        text = str(response.get("content") or response.get("text") or "")
    else:
        text = str(getattr(response, "content", "") or "")
    return text.strip()


def _build_prompt(summary: str, turns: list[dict]) -> str:
    lines = []
    if summary:
        lines.append("【已有长期记忆】")
        lines.append(summary[:SUMMARY_KEEP_CHARS])
        lines.append("")
    lines.append("【需要压缩进长期记忆的对话】")
    for turn in turns:
        user = str(turn.get("user") or "").strip()
        assistant = str(turn.get("assistant") or "").strip()
        if user:
            lines.append(f"用户：{user[:TURN_CHARS]}")
        if assistant:
            lines.append(f"助手：{assistant[:TURN_CHARS]}")
    lines.append("")
    lines.append("请把这些内容与已有长期记忆合并成一段新的长期记忆，保留：用户偏好、重要事实、"
                 "未完成的约定与结论；去掉寒暄与重复。")
    return "\n".join(lines)


def search(user_id: str, session_id: str, agent_id: str = "main") -> list[dict]:
    """返回 [{role, content}]：长期摘要（若有）+ 最近 max_messages 条消息。"""
    data = _load(_file_for(user_id, session_id, agent_id))
    turns = data.get("turns") or []
    summary = str(data.get("summary") or "").strip()

    messages: list[dict] = []
    if summary:
        messages.append({"role": "system", "content": f"【长期记忆】{summary[:SUMMARY_MAX_CHARS]}"})

    # 窗口大小以「写入时记录的值」为准（不同 agent 可能配置不同），没有则用 env/默认
    limit = int(data.get("max_messages") or _max_messages())
    limit = max(1, limit)
    flat: list[dict] = []
    for t in turns:
        if not isinstance(t, dict):
            continue
        if t.get("user"):
            flat.append({"role": "user", "content": str(t["user"])})
        if t.get("assistant"):
            flat.append({"role": "assistant", "content": str(t["assistant"])})
    messages.extend(flat[-limit:] if limit < len(flat) else flat)
    return messages


def append(
    user_id: str,
    session_id: str,
    agent_id: str,
    user_message: str,
    assistant_message: str,
    commit_limit: int = 0,
    max_messages: int = 0,
) -> bool:
    """追加一个回合；达到 commit_limit 时压缩早期回合进长期摘要。

    - `commit_limit<=0`：只做窗口滚动（①档）；
    - `commit_limit>0`：未压缩回合数够多就压（②档）。
    """
    path = _file_for(user_id, session_id, agent_id)
    data = _load(path)
    turns: list[dict] = list(data.get("turns") or [])
    committed = int(data.get("committed") or 0)
    summary = str(data.get("summary") or "")

    turns.append({
        "user": user_message or "",
        "assistant": assistant_message or "",
        "ts": datetime.now().isoformat(),
    })

    window = _window_turns(int(max_messages or _max_messages()))
    try:
        commit = int(commit_limit)
    except (TypeError, ValueError):
        commit = 0

    # ②档：未压缩回合达到阈值 → 把窗口外的回合交给 reader 压缩
    if commit > 0 and len(turns) > window and len(turns) - window >= commit:
        pending = turns[:-window]
        try:
            new_summary = _summarize(_build_prompt(summary, pending))
        except Exception as exc:  # noqa: BLE001
            # 压缩失败不能丢数据：保留原文，下次再压
            debug_log(f"[local_context] 压缩失败（保留原文）: {exc}")
            new_summary = ""
        if new_summary:
            summary = new_summary[:SUMMARY_MAX_CHARS]
            committed += len(pending)
            turns = turns[-window:]

    # ①档：无论如何都只保留窗口内的内容（压缩成功时窗口是最近 limit 条）
    keep = window + (commit if commit > 0 else 0)
    turns = turns[-keep:]

    payload = {
        "max_messages": int(max_messages or _max_messages()),
        "turns": turns,
        "summary": summary,
        "committed": committed,
        "compressed_at": datetime.now().isoformat() if summary else str(data.get("compressed_at") or ""),
    }
    try:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    except Exception as exc:  # noqa: BLE001
        debug_log(f"[local_context] 写入失败: {exc}")
        return False

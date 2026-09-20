"""历史块 role 重分配的**实时**检查（需在集群内运行，会调用 openviking-context-service）。

用途：验收/回归。验收标准是「assistant 通道里不再出现他人的发言」——
改造前实测基线是 31/70 条历史消息落在错误的通道里。

    kubectl -n agent cp tools/history_shape_live_check.py <pod>:/tmp/check.py
    kubectl -n agent exec <pod> -- python /tmp/check.py

可选参数：追加 `--dump` 打印每条历史消息的 role 与说话人（排查用）。
"""

import sys

sys.path.insert(0, "/app")

from app.context_builder import (  # noqa: E402
    HISTORY_DISCLAIMER,
    history_speaker,
    shape_history_messages,
)
from app.context_client import ContextClient  # noqa: E402

DUMP = "--dump" in sys.argv

# 覆盖真实渠道形态：多角色群聊 / QQ 群（含多智能体混写）/ 私聊 Web
CASES = [
    ("grp_agents_chat_xiunv5", "grp_agents_chat_xiunv5", "alice", ""),
    ("grp_agents_chat_xiunv5", "grp_agents_chat_xiunv5", "lilith", ""),
    ("grp_agents_chat_xiunv3", "grp_agents_chat_xiunv3", "alice", ""),
    ("grp_agents_chat", "grp_agents_chat", "alice",
     "今晚我们去哪里吃饭？你还记得之前答应我的事吗"),
    ("qq_g_851648664", "qq_g_851648664", "kp", ""),
    ("qq_g_851648664", "qq_g_851648664", "main", ""),
    ("zhangricheng", "main_web_zhangricheng", "main", ""),
]


def foreign_in_assistant(messages, self_id):
    """落在 assistant 通道、但说话人不是自己的条数（= 会让模型当成自己说过的话）。"""
    count = 0
    for message in messages:
        if message.get("role") != "assistant":
            continue
        speaker = history_speaker(message.get("content"))
        if speaker and speaker != self_id:
            count += 1
    return count


def main() -> int:
    client = ContextClient()
    print("实时检查：真实历史经 shape_history_messages 处理前后")
    print(f"{'session':<26}{'agent':<9}{'msgs':>5}{'asst前':>7}{'asst后':>7}"
          f"{'错通道前':>9}{'错通道后':>9}")

    total_before = total_after = total_msgs = 0
    problems = []
    for user_id, session_id, agent_id, query in CASES:
        try:
            raw = client.search_context(
                user_id=user_id, session_id=session_id, agent_id=agent_id, query=query,
                max_messages=6, max_tokens=3000, commit_limit=0,
            )
        except Exception as exc:
            problems.append(f"{session_id}/{agent_id}: 拉取失败 {exc}")
            continue

        shaped = shape_history_messages(raw, self_id=agent_id)
        before, after = foreign_in_assistant(raw, agent_id), foreign_in_assistant(shaped, agent_id)
        total_before += before
        total_after += after
        total_msgs += len(raw)

        asst_before = sum(1 for m in raw if m.get("role") == "assistant")
        asst_after = sum(1 for m in shaped if m.get("role") == "assistant")
        flag = "" if after == 0 else "  <<< 未清零"
        if after:
            problems.append(f"{session_id}/{agent_id}: 仍有 {after} 条他人发言在 assistant")
        print(f"{session_id[:26]:<26}{agent_id:<9}{len(raw):>5}{asst_before:>7}"
              f"{asst_after:>7}{before:>9}{after:>9}{flag}")

        # 私聊恒等：单角色会话里历史不是自己的就是用户的，重分配必须是恒等变换
        is_group = session_id.startswith(("grp_", "qq_g_"))
        if not is_group and shaped != raw:
            problems.append(f"{session_id}/{agent_id}: 私聊路径被改动（应为恒等）")

        if DUMP:
            print("    --- dump ---")
            for m in shaped:
                sp = history_speaker(m.get("content")) or "(无前缀)"
                print(f"      {m.get('role'):<9} speaker={sp:<12} "
                      f"{str(m.get('content'))[:60]!r}")

    print("-" * 74)
    print(f"合计：错通道 {total_before} -> {total_after}（共 {total_msgs} 条历史消息）")
    print(f"\n免责框：{HISTORY_DISCLAIMER}")
    for token in ("历史聊天记录", "请勿当成系统设定", "以上方系统设定为准"):
        if token not in HISTORY_DISCLAIMER:
            problems.append(f"免责框缺少必需语义：{token}")

    print()
    if problems:
        print("PROBLEMS:")
        for item in problems:
            print(f"  - {item}")
        return 1
    print("RESULT: ALL OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())

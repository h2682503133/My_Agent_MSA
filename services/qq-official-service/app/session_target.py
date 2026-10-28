"""会话 id ↔ 推送目标：纯函数，不依赖 satori / aiohttp，便于单测。

推送目标从 session_id **直接重建**，不依赖"用户先发过一条消息"而缓存下来的 event
对象（进程内缓存重启即空，定时提醒/离线转投/缓冲补发会因此静默丢失）。

支持带渠道前缀的形态：`qq_…` / `qq_g_…` / `qqofficial_…` / `qqofficial_g_…`。
"""


def parse_session_target(session_id: str) -> tuple[str, str] | None:
    """把会话 id 解析成推送目标：("private", id) / ("group", id)。

    兼容的形态（前导渠道前缀会被剥掉）：

        qq_2195239690            → ("private", "2195239690")     # 主渠道
        qq_g_12345               → ("group", "12345")
        qqofficial_2195239690    → ("private", "2195239690")     # 定时任务/转投
        qqofficial_4F9ECB5FB5…   → ("private", "4F9ECB5FB5…")    # openid 直传
        qqofficial_g_12345       → ("group", "12345")

    解析不出返回 None（调用方回报投递失败）。
    """
    key = str(session_id or "").strip()
    if not key:
        return None

    # 剥渠道前缀：长的先剥，避免 "qqofficial_" 被 "qq_" 抢走
    for prefix in ("qqofficial_g_", "qqofficial_", "qqo_", "qq_g_", "qq_"):
        if key.startswith(prefix):
            key = key[len(prefix):].strip()
            is_group = prefix.endswith("_g_")
            break
    else:
        is_group = False

    if not key:
        return None
    if is_group:
        return ("group", key)
    # 无前缀且以 g_ 开头：视为群
    if key.startswith("g_"):
        gid = key[len("g_"):].strip()
        return ("group", gid) if gid else None
    return ("private", key)

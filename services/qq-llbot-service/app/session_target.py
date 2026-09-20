"""会话 id ↔ 推送目标：纯函数，不依赖 satori / aiohttp，便于单测。

会话 id 的两种形式由 `qq_bridge._session_key` 确定性产出：

    私聊: qq_{QQ号}
    群聊: qq_g_{群号}

所以推送目标可以从 session_id **直接重建**，不需要依赖"用户先发过一条消息"而缓存下来的
event 对象。这一点很关键：`QQBridge._sessions` 是进程内字典，只在收到消息时填充，
服务重启后必然为空 —— 那时所有被动推送（定时提醒、离线转投、缓冲补发）都会以
"会话不在线"被静默丢弃。
"""


def parse_session_target(session_id: str) -> tuple[str, str] | None:
    """把会话 id 解析成推送目标：("private", QQ号) / ("group", 群号)。

    解析不出目标返回 None（不认识的会话格式 → 无法送达，调用方应回报投递失败）。
    注意 `qq_g_` 必须**先于** `qq_` 判断，否则群会话会被误判成私聊。
    """
    key = str(session_id or "").strip()
    if not key:
        return None
    if key.startswith("qq_g_"):
        group_id = key[len("qq_g_"):].strip()
        return ("group", group_id) if group_id else None
    if key.startswith("qq_"):
        qq_id = key[len("qq_"):].strip()
        return ("private", qq_id) if qq_id else None
    return None

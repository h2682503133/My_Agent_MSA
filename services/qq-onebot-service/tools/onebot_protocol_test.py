"""OneBot 协议层自检（离线，不连网络）。"""

from __future__ import annotations

import sys
from pathlib import Path

SERVICE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVICE))

from app import onebot_protocol as p  # noqa: E402

FAILS: list[str] = []
CHECKS = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global CHECKS
    CHECKS += 1
    if cond:
        print(f"  ok    {name}")
    else:
        FAILS.append(name)
        print(f"  FAIL  {name} {extra}")


PRIVATE = {
    "post_type": "message", "message_type": "private", "user_id": 123456,
    "message_id": 11, "self_id": 10001,
    "message": [
        {"type": "text", "data": {"text": "你好"}},
        {"type": "image", "data": {"url": "http://img/1.png"}},
    ],
}
GROUP_AT = {
    "post_type": "message", "message_type": "group", "group_id": 555, "user_id": 123456,
    "message": [
        {"type": "at", "data": {"qq": "10001"}},
        {"type": "text", "data": {"text": "帮我看下"}},
    ],
}
GROUP_NO_AT = {
    "post_type": "message", "message_type": "group", "group_id": 555, "user_id": 123456,
    "message": [{"type": "text", "data": {"text": "闲聊"}}],
}
GROUP_FILE = {
    "post_type": "message", "message_type": "group", "group_id": 555, "user_id": 123456,
    "message": [
        {"type": "at", "data": {"qq": "10001"}},
        {"type": "file", "data": {"name": "报告.pdf", "url": "http://f/1.pdf", "file_size": 2048}},
    ],
}


def main() -> int:
    print("事件解析")
    parsed_private = p.parse_event(PRIVATE, "10001")
    check("私聊 session_id", parsed_private.session_id == "qq_123456", parsed_private.session_id)
    check("私聊文本（图片记为占位）", parsed_private.text == "你好\n[图片]", parsed_private.text)
    check("私聊图片列表", parsed_private.images == ["http://img/1.png"], str(parsed_private.images))
    check("私聊不需要 @", parsed_private.at_me is False)

    group_at = p.parse_event(GROUP_AT, "10001")
    check("群聊 session_id", group_at.session_id == "qq_g_555", group_at.session_id)
    check("群聊 @ 检测", group_at.at_me is True)
    check("群聊文本不含 at", group_at.text == "帮我看下", group_at.text)

    group_no_at = p.parse_event(GROUP_NO_AT, "10001")
    check("未 @ 的群消息", group_no_at.at_me is False)

    file_msg = p.parse_event(GROUP_FILE, "10001")
    check("文件段被识别", file_msg.files and file_msg.files[0]["name"] == "报告.pdf", str(file_msg.files))
    check("文件 url", file_msg.files[0]["url"] == "http://f/1.pdf")

    print("响应判定")
    check("私聊一律响应", p.should_respond(parsed_private, True) is True)
    check("群聊默认要 @", p.should_respond(group_no_at, True) is False)
    check("@ 了的群消息响应", p.should_respond(group_at, True) is True)
    check("关掉要求后群聊都响应", p.should_respond(group_no_at, False) is True)

    print("非消息事件")
    check("notice 忽略", p.parse_event({"post_type": "notice"}, "") is None)
    check("meta_event 忽略", p.parse_event({"post_type": "meta_event"}, "") is None)
    check("message_sent 忽略", p.parse_event({"post_type": "message_sent"}, "") is None)

    print("发送动作")
    action, params = p.build_send_payload("qq_g_555", "回复", ["http://img/2.png"])
    check("群发送 action", action == "send_group_msg", action)
    check("group_id 数字", params["group_id"] == 555, str(params))
    check("消息段顺序：文本在前", params["message"][0]["type"] == "text")
    check("图片段 file=url", params["message"][1]["data"]["file"] == "http://img/2.png")

    action2, params2 = p.build_send_payload("qq_123456", "私聊")
    check("私发送 action", action2 == "send_private_msg", action2)
    check("user_id 数字", params2["user_id"] == 123456)

    try:
        p.build_send_payload("weird_id", "x")
        check("未知 session 抛错", False, "没有抛错")
    except ValueError:
        check("未知 session 抛错", True)

    print("反向 WS 鉴权")
    check("无 token 放行", p.authorized({}, {}, "") is True)
    check("Bearer 正确", p.authorized({"Authorization": "Bearer t"}, {}, "t") is True)
    check("access_token 正确", p.authorized({}, {"access_token": "t"}, "t") is True)
    check("错误 token 拒绝", p.authorized({"Authorization": "Bearer x"}, {}, "t") is False)
    check("大小写不敏感头名", p.authorized({"authorization": "bearer t"}, {}, "t") is True)

    print("session 反解析")
    check("群 session", p.parse_session_target("qq_g_555") == ("group", "555"))
    check("私聊 session", p.parse_session_target("qq_123456") == ("private", "123456"))
    check("未知格式", p.parse_session_target("abc") is None)

    print()
    if FAILS:
        print(f"FAILED {len(FAILS)}: {FAILS}")
        return 1
    print(f"ALL OK — {CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

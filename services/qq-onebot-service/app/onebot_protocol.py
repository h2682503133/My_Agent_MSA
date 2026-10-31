"""OneBot 11 消息解析与协议细节（纯函数，便于离线单测）。

只覆盖我们真正用到的部分：
- 事件：post_type=message（private / group）、message_sent 忽略、notice/meta_event 忽略
- 消息段：text / at / image / face / reply / file / mface
- 发送：send_private_msg / send_group_msg（图片用 image 段 + file=url）
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# 群里除了 @ 机器人，这些"开场白"也算叫它（可扩展）
WAKE_WORDS = ("", "，")


@dataclass
class ParsedMessage:
    user_id: str = ""
    session_id: str = ""
    text: str = ""
    images: list[str] = field(default_factory=list)
    files: list[dict] = field(default_factory=list)
    at_me: bool = False
    message_type: str = "private"   # private | group
    message_id: str = ""
    raw: dict = field(default_factory=dict)


def session_id_for(message_type: str, user_id: str, group_id: str) -> str:
    """会话 id 与 satori 那条保持一致：私聊 qq_<QQ>、群聊 qq_g_<群号>。"""
    if message_type == "group" and group_id:
        return f"qq_g_{group_id}"
    return f"qq_{user_id}"


def parse_session_target(session_id: str) -> tuple[str, str] | None:
    """会话 id → ("private", QQ) / ("group", 群号)；不认识返回 None。"""
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


def _segment_data(segment: dict) -> dict:
    data = segment.get("data")
    return data if isinstance(data, dict) else {}


def _clean_text(text: str) -> str:
    # OneBot 常见 CQ 码残留（有些实现对混合文本仍会带 CQ 码）
    text = re.sub(r"\[CQ:[^\]]+\]", "", text or "")
    return text.replace("\r\n", "\n").strip()


def parse_event(event: dict, self_id: str = "") -> ParsedMessage | None:
    """把 OneBot 事件转成 ParsedMessage；不是消息事件时返回 None。"""
    if not isinstance(event, dict):
        return None
    if event.get("post_type") != "message":
        return None

    message_type = str(event.get("message_type") or "private")
    sender = event.get("sender") if isinstance(event.get("sender"), dict) else {}
    user_id = str(event.get("user_id") or sender.get("user_id") or "")
    group_id = str(event.get("group_id") or "")
    if not user_id:
        return None

    raw_message = event.get("message")
    segments: list[dict] = []
    text_parts: list[str] = []

    if isinstance(raw_message, str):
        text_parts.append(_clean_text(raw_message))
    elif isinstance(raw_message, list):
        for segment in raw_message:
            if not isinstance(segment, dict):
                continue
            segments.append(segment)
            seg_type = str(segment.get("type") or "")
            data = _segment_data(segment)
            if seg_type == "text":
                text_parts.append(str(data.get("text") or ""))
            elif seg_type == "face":
                text_parts.append("[表情]")
            elif seg_type == "mface":
                text_parts.append("[表情]")
            elif seg_type == "image":
                text_parts.append("[图片]")
            elif seg_type == "file":
                text_parts.append("[文件]")
            # at / reply / forward 等不产生正文

    images: list[str] = []
    files: list[dict] = []
    at_me = False
    for segment in segments:
        seg_type = str(segment.get("type") or "")
        data = _segment_data(segment)
        if seg_type == "at":
            target = str(data.get("qq") or "")
            if self_id and target == str(self_id):
                at_me = True
        elif seg_type == "image":
            url = str(data.get("url") or data.get("file") or "").strip()
            if url and url not in images:
                images.append(url)
        elif seg_type == "file":
            files.append({
                "name": str(data.get("name") or data.get("file") or "file"),
                "url": str(data.get("url") or ""),
                "size": data.get("file_size") or data.get("size") or 0,
            })

    text = "\n".join(part for part in text_parts if part is not None)
    text = _clean_text(text)

    return ParsedMessage(
        user_id=user_id,
        session_id=session_id_for(message_type, user_id, group_id),
        text=text,
        images=images,
        files=files,
        at_me=at_me,
        message_type=message_type,
        message_id=str(event.get("message_id") or ""),
        raw=event,
    )


def should_respond(parsed: ParsedMessage, group_require_at: bool = True) -> bool:
    """私聊一律响应；群聊看是否 @ 了机器人（可配置）。"""
    if parsed.message_type != "group":
        return True
    if not group_require_at:
        return True
    return parsed.at_me


def build_send_payload(session_id: str, text: str = "", images: list[str] | None = None) -> tuple[str, dict]:
    """构造发送动作：返回 (action, params)。"""
    target = parse_session_target(session_id)
    if target is None:
        raise ValueError(f"无法识别的会话 id：{session_id}")
    kind, ident = target

    message: list[dict] = []
    if text:
        message.append({"type": "text", "data": {"text": text}})
    for url in images or []:
        if not url:
            continue
        message.append({"type": "image", "data": {"file": url}})
    if not message:
        message.append({"type": "text", "data": {"text": ""}})

    if kind == "group":
        return "send_group_msg", {"group_id": int(ident) if ident.isdigit() else ident, "message": message}
    return "send_private_msg", {"user_id": int(ident) if ident.isdigit() else ident, "message": message}


def authorized(headers: dict, query: dict, token: str) -> bool:
    """反向 WS 的鉴权：Bearer / access_token 查询参数（NapCat 两种都支持）。"""
    if not token:
        return True

    auth = ""
    for key, value in (headers or {}).items():
        if str(key).lower() == "authorization":
            auth = str(value or "").strip()
            break
    if auth.lower().startswith("bearer "):
        auth = auth[7:].strip()
    if auth and auth == token:
        return True

    supplied = ""
    for key, value in (query or {}).items():
        if str(key).lower() == "access_token":
            supplied = str(value or "").strip()
            break
    return bool(supplied) and supplied == token

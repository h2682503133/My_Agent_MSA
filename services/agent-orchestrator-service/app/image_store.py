"""用户发来的图片：自动落盘 + 提示词/历史里的图片引用。

为什么要有这一层
----------------
图片原先只以 base64 data URL 活在**本轮任务**里（`task.images` → 模型 messages），
任务一结束就没了：openviking 里只写了 `user_message=task.content` 纯文本，
下一轮模型既看不到图，也不知道曾经有过图。

现在的做法：
- **落盘**（本模块，框架行为，不是工具）：收到消息时把每张图写到
  `<用户工作区>/uploads/images/<YYYYMMDD>/<sha1 前 16 位>.<ext>`
  - 用内容哈希命名 → 同一张图重复发送天然复用同一个文件（不重复占盘）
  - 按日期分子目录 → 单目录文件数可控，便于 `dir-list uploads/images/`
- **引用**：`task.image_refs` 带上工作区相对路径，提示词与历史里都写
  `（附图: uploads/images/20260101/abcd...jpg）`，模型因此知道图在哪，
  需要回看时调用 `image-view`（tool-runtime）重新载入像素。

失败闭合：任何一张图存不下来都只记日志、不影响本轮对话
（宁可少一个引用，也不能让用户的消息发不出去）。
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import os
import re
from pathlib import Path

# 用户工作区内的固定目录（相对用户根目录），按日期再分子目录
IMAGE_SUBDIR = "uploads/images"

MIME_BY_EXT = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}
EXT_BY_MIME = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
}

# 文件头兜底：data URL 里的 mime 不可信（有些渠道会写 application/octet-stream）
_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"\xff\xd8\xff", ".jpg"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
    (b"BM", ".bmp"),
)

_SAFE_SEGMENT_RE = re.compile(r"[^0-9A-Za-z_.@-]+")


def safe_user_segment(user_id: str | None, default: str = "default") -> str:
    """user_id → 安全目录名（与 tool_runtime_client 的规则保持一致）。"""
    raw = str(user_id or default).strip() or default
    safe = _SAFE_SEGMENT_RE.sub("_", raw).strip("._") or default
    if safe in {".", ".."}:
        safe = default
    return safe


def sniff_ext(data: bytes) -> str:
    """按文件头判断扩展名，认不出返回空串。"""
    head = data[:16]
    for magic, ext in _MAGIC:
        if head.startswith(magic):
            return ext
    if len(head) >= 12 and head[0:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp"
    return ""


def parse_data_url(value: str) -> tuple[bytes, str] | None:
    """解析 `data:image/jpeg;base64,...`。

    返回 (原始字节, 扩展名)；不是可识别的 data URL 返回 None（由调用方跳过）。
    扩展名优先级：真实文件头 > data URL 的 mime（渠道给的 mime 不一定准）。
    """
    raw = str(value or "").strip()
    if not raw.startswith("data:"):
        return None
    header, _, payload = raw.partition(",")
    if not payload:
        return None
    if ";base64" not in header.lower():
        return None

    mime = header[len("data:"):].split(";")[0].strip().lower()
    compact = "".join(payload.split())
    compact += "=" * (-len(compact) % 4)
    try:
        data = base64.b64decode(compact, validate=False)
    except Exception:
        return None
    if not data:
        return None

    ext = sniff_ext(data) or EXT_BY_MIME.get(mime, "")
    if not ext:
        return None
    return data, ext


def user_workspace_dir(workspace_root, user_id: str) -> Path:
    return Path(workspace_root) / "users" / safe_user_segment(user_id)


def _image_root(workspace_root, user_id: str) -> Path:
    return user_workspace_dir(workspace_root, user_id) / IMAGE_SUBDIR


def _find_existing(image_root: Path, digest: str) -> str | None:
    """按内容哈希查重（跨日期目录）：同一张图只存一份。"""
    for candidate in sorted(image_root.glob(f"*/{digest}.*")):
        if candidate.is_file():
            return f"{IMAGE_SUBDIR}/{candidate.parent.name}/{candidate.name}"
    return None


def store_image(workspace_root, user_id: str, value: str) -> str | None:
    """把一张图（data URL）存进用户工作区，返回工作区相对路径。

    返回的路径以**用户工作区根目录**为基准（如 `uploads/images/20260101/ab.jpg`），
    与 tool-runtime 的 workspace_dir 口径一致，可直接传给 image-view / dir-list。
    """
    parsed = parse_data_url(value)
    if not parsed:
        return None
    data, ext = parsed
    digest = hashlib.sha1(data).hexdigest()[:16]

    image_root = _image_root(workspace_root, user_id)
    existing = _find_existing(image_root, digest)
    if existing:
        return existing

    day = datetime.datetime.now().strftime("%Y%m%d")
    target = image_root / day / f"{digest}{ext}"
    target.parent.mkdir(parents=True, exist_ok=True)
    # 临时名带 pid，避免多个副本/线程同时落同一张图时互相踩掉 .tmp
    tmp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    tmp.write_bytes(data)
    tmp.replace(target)
    return f"{IMAGE_SUBDIR}/{day}/{target.name}"


def store_images(workspace_root, user_id: str, images) -> list[str]:
    """批量落盘，返回成功保存的引用列表（失败的单张直接跳过，不影响其它图）。"""
    refs: list[str] = []
    for value in images or []:
        try:
            ref = store_image(workspace_root, user_id, value)
        except Exception:
            ref = None
        if ref and ref not in refs:
            refs.append(ref)
    return refs


def image_marker(refs) -> str:
    """提示词 / 历史里跟在用户消息后的图片引用（无引用时返回空串）。"""
    paths = [str(r).strip() for r in (refs or []) if str(r).strip()]
    if not paths:
        return ""
    return "\n（附图: " + "、".join(paths) + "）"

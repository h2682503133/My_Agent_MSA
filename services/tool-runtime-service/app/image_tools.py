"""图片读取工具（image-view）。

职责划分（重要）：
- 图片的**保存**不是工具，而是框架的自动行为：orchestrator 收到带图消息时会把
  data URL 落到 `<用户工作区>/uploads/images/<YYYYMMDD>/<sha1>.<ext>`
  （实现见 agent-orchestrator-service/app/image_store.py）。
- 这里只负责"把已经存在的图片读出来给模型看"：读文件（或下载 URL）→ 探测真实
  格式与尺寸 → 转 data URL → 由 server.py 以 `model_image` 事件回传给 orchestrator，
  orchestrator 再把它挂到下一轮模型调用的 images 上。

依赖只用标准库 + requests（镜像里已有），不做像素级处理：
- 不缩放、不转码：原样传 data URL。缩放交给上游模型服务端（各家都会自己 clamp），
  这样不会引入 Pillow 这类重依赖，也不会二次损失画质。
- 体积上限 `IMAGE_VIEW_MAX_BYTES`（默认 7.5MB，base64 膨胀后约 10MB，
  与主流多模态接口的单图上限对齐）：超限直接报错，让模型改用缩小后的图片，
  而不是把超限请求丢给上游去失败。
"""

from __future__ import annotations

import base64
import time
from dataclasses import dataclass
from pathlib import Path

import requests

from app import config
from app.logger import log
from app.workspace import safe_path

# 常见图片后缀 → MIME
MIME_BY_EXT = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}

# 文件头 → (MIME, 推荐后缀)：以真实字节为准，文件后缀只是兜底
_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "image/png", ".png"),
    (b"\xff\xd8\xff", "image/jpeg", ".jpg"),
    (b"GIF87a", "image/gif", ".gif"),
    (b"GIF89a", "image/gif", ".gif"),
    (b"BM", "image/bmp", ".bmp"),
)

IMAGE_EXTENSIONS = frozenset(MIME_BY_EXT)


@dataclass
class ImagePayload:
    """一张准备好交给模型的图片。"""

    relpath: str          # 工作区相对路径（远程 URL 时为原 URL）
    mime: str
    ext: str
    width: int
    height: int
    size: int
    data_url: str

    @property
    def size_text(self) -> str:
        return format_bytes(self.size)

    @property
    def dimension_text(self) -> str:
        if self.width and self.height:
            return f"{self.width}x{self.height}"
        return "尺寸未知"


def format_bytes(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.2f} MB"


def sniff_magic(data: bytes):
    """按文件头判断真实图片类型，返回 (mime, ext)；不是支持的图片返回 None。"""
    head = data[:16]
    for magic, mime, ext in _MAGIC:
        if head.startswith(magic):
            return mime, ext
    # WebP: RIFF....WEBP
    if len(head) >= 12 and head[0:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp", ".webp"
    return None


def ext_from_name(name: str) -> str:
    """按文件名后缀推断扩展名（大小写不敏感），未知返回空串。"""
    suffix = Path(str(name or "")).suffix.lower()
    return suffix if suffix in MIME_BY_EXT else ""


def _jpeg_dimensions(data: bytes):
    total = len(data)
    index = 2  # 跳过 SOI
    while index + 3 < total:
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        # 无长度字段的标记
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            index += 2
            continue
        if marker == 0xD9 or marker == 0xDA:  # EOI / SOS：之后不会再有 SOF
            return None
        seg_len = int.from_bytes(data[index + 2:index + 4], "big")
        if seg_len < 2:
            return None
        # SOF0-SOF15（排除 DHT/JPG/DAC）里带尺寸
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            if index + 9 > total:
                return None
            height = int.from_bytes(data[index + 5:index + 7], "big")
            width = int.from_bytes(data[index + 7:index + 9], "big")
            return (width, height)
        index += 2 + seg_len
    return None


def _webp_dimensions(data: bytes):
    total = len(data)
    if total < 30:
        return None
    chunk = data[12:16]
    if chunk == b"VP8X":
        width = int.from_bytes(data[24:27], "little") + 1
        height = int.from_bytes(data[27:30], "little") + 1
        return (width, height)
    if chunk == b"VP8 ":
        marker = data.find(b"\x9d\x01\x2a", 20, 40)
        if marker < 0 or marker + 7 > total:
            return None
        width = int.from_bytes(data[marker + 3:marker + 5], "little") & 0x3FFF
        height = int.from_bytes(data[marker + 5:marker + 7], "little") & 0x3FFF
        return (width, height)
    if chunk == b"VP8L":
        if data[20] != 0x2F or total < 25:
            return None
        bits = int.from_bytes(data[21:25], "little")
        width = (bits & 0x3FFF) + 1
        height = ((bits >> 14) & 0x3FFF) + 1
        return (width, height)
    return None


def image_dimensions(data: bytes):
    """读取图片宽高（PNG/JPEG/GIF/BMP/WebP），失败返回 None。"""
    sniffed = sniff_magic(data)
    if not sniffed:
        return None
    mime = sniffed[0]
    try:
        if mime == "image/png":
            if len(data) < 24:
                return None
            return (
                int.from_bytes(data[16:20], "big"),
                int.from_bytes(data[20:24], "big"),
            )
        if mime == "image/gif":
            if len(data) < 10:
                return None
            return (
                int.from_bytes(data[6:8], "little"),
                int.from_bytes(data[8:10], "little"),
            )
        if mime == "image/bmp":
            if len(data) < 26:
                return None
            return (
                abs(int.from_bytes(data[18:22], "little", signed=True)),
                abs(int.from_bytes(data[22:26], "little", signed=True)),
            )
        if mime == "image/jpeg":
            return _jpeg_dimensions(data)
        if mime == "image/webp":
            return _webp_dimensions(data)
    except Exception:
        return None
    return None


# 集群 DNS 偶发抖动（CoreDNS 上游超时）会让"已经生成好的图"下载失败，
# 这类错误重试两次基本能过；真失败也不能丢图，调用方会降级成直接发远程 URL。
_TRANSIENT_HINTS = (
    "name resolution",
    "temporary failure",
    "cannot connect",
    "connection aborted",
    "connection reset",
    "timed out",
    "timeout",
)


def _looks_transient(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(hint in text for hint in _TRANSIENT_HINTS)


def _download_once(url: str, max_bytes: int):
    response = requests.get(
        url,
        timeout=config.IMAGE_VIEW_TIMEOUT_SECONDS,
        stream=True,
        allow_redirects=True,
        headers={"User-Agent": "my-agent-tool-runtime/image-view"},
    )
    response.raise_for_status()
    buffer = bytearray()
    for chunk in response.iter_content(chunk_size=64 * 1024):
        buffer.extend(chunk)
        if len(buffer) > max_bytes:
            raise ValueError(
                f"图片过大：超过 {format_bytes(max_bytes)} 上限（{url}）"
            )
    content_type = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    return bytes(buffer), content_type


def _download(url: str, max_bytes: int) -> tuple[bytes, str]:
    """下载远程图片（瞬时网络错误重试 2 次），超过 max_bytes 立即中断。"""
    last_error: Exception | None = None
    for attempt, delay in enumerate((0.0, 0.5, 1.5)):
        if delay:
            time.sleep(delay)
        try:
            return _download_once(url, max_bytes)
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if not _looks_transient(exc):
                raise
            log(f"[image] 下载重试 {attempt + 1}/3 失败: {exc}")
    raise last_error if last_error else RuntimeError("下载失败")


def _read_local(target: str, root: Path, max_bytes: int) -> tuple[bytes, str]:
    path = safe_path(root, target)
    if not path.exists():
        raise FileNotFoundError(f"文件不存在：{target}")
    if not path.is_file():
        raise IsADirectoryError(f"不是文件：{target}")
    size = path.stat().st_size
    if size > max_bytes:
        raise ValueError(
            f"图片过大：{format_bytes(size)} 超过 {format_bytes(max_bytes)} 上限（{target}）"
        )
    return path.read_bytes(), ext_from_name(path.name)


def load_image(target: str, root: Path, max_bytes: int | None = None) -> ImagePayload:
    """把工作区相对路径或 http(s) URL 读成可交给模型的 data URL。

    失败一律抛异常（由调用方转成给模型看的错误文本），不静默返回空图。
    """
    raw = str(target or "").strip()
    if not raw:
        raise ValueError("缺少图片路径（用法：image-view|图片路径|需求方向）")

    limit = int(max_bytes or config.IMAGE_VIEW_MAX_BYTES)

    if raw.lower().startswith(("http://", "https://")):
        data, content_type = _download(raw, limit)
        relpath = raw
        name_ext = ext_from_name(raw.split("?")[0])
        fallback_mime = content_type if content_type.startswith("image/") else ""
    else:
        data, name_ext = _read_local(raw, root, limit)
        relpath = str(safe_path(root, raw).relative_to(root)).replace("\\", "/")
        fallback_mime = ""

    if not data:
        raise ValueError(f"图片内容为空：{raw}")

    sniffed = sniff_magic(data)
    if sniffed:
        mime, ext = sniffed
    elif name_ext:
        mime, ext = MIME_BY_EXT[name_ext], name_ext
    elif fallback_mime:
        mime = fallback_mime
        ext = next((e for e, m in MIME_BY_EXT.items() if m == fallback_mime), "")
    else:
        raise ValueError(f"不是支持的图片格式（png/jpg/gif/webp/bmp）：{raw}")

    width, height = image_dimensions(data) or (0, 0)
    data_url = f"data:{mime};base64," + _b64(data)
    return ImagePayload(
        relpath=relpath,
        mime=mime,
        ext=ext,
        width=width,
        height=height,
        size=len(data),
        data_url=data_url,
    )


def save_generated_image(root: Path, value: str, index: int = 0) -> tuple[str, str, str]:
    """把生成的图片（远程 URL 或 data URL）存进工作区 outputs/images/<日期>/。

    返回 (相对路径, data_url, 错误)。成功时错误为空串。
    与 image-view 的 load_image 复用同一套下载/解码/嗅探逻辑，避免两套实现漂移。
    """
    import datetime
    import hashlib

    try:
        payload = load_image(value, root, None)
    except Exception as exc:  # noqa: BLE001
        return "", "", str(exc)

    raw = payload.data_url.split(",", 1)[-1]
    import base64

    try:
        data = base64.b64decode(raw)
    except Exception as exc:  # noqa: BLE001
        return "", "", f"解码失败: {exc}"

    digest = hashlib.sha256(data).hexdigest()[:16]
    stamp = datetime.date.today().strftime("%Y%m%d")
    target_dir = Path(root) / "outputs" / "images" / stamp
    target_dir.mkdir(parents=True, exist_ok=True)
    ext = payload.ext if getattr(payload, "ext", "") else "png"
    target = target_dir / f"gen-{digest}.{ext}"
    target.write_bytes(data)
    try:
        rel = str(target.relative_to(Path(root))).replace("\\", "/")
    except ValueError:
        rel = str(target)
    return rel, f"data:image/{'jpeg' if ext == 'jpg' else ext};base64,{base64.b64encode(data).decode('ascii')}", ""


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")

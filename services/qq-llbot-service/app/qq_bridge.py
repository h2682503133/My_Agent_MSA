"""
QQ LLBot 桥接：通过 Satori 协议收发 QQ 消息。

参考旧架构 core/gateway/qq_bridge.py 实现，适配微服务架构：
- 接收 QQ 消息 → 调用 scheduler.CreateTask(channel="qq")
- 订阅 scheduler 事件 → 收到 assistant_message → 推送回 QQ

群聊支持：
- 私聊消息直接下发；
- 群聊消息仅在被明确 @ 机器人时才下发（@全体 不触发）；
- 会话以会话 key 区分：私聊为 qq_{user_id}，群聊为 qq_g_{channel_id}，
  保证不同群的上下文互不干扰，回复也发回原群。
"""
import asyncio
import base64
import json
import mimetypes
import re
from pathlib import Path
from typing import Callable, Awaitable
from urllib.parse import urlparse, urlunparse

import aiohttp
from satori.client import App, WebsocketsInfo, Account
from satori.const import EventType
from satori.event import MessageEvent
from satori.element import At, Text, Image, File, Audio, Video

from satori.model import ChannelType

from app import config
from app.session_target import parse_session_target as _parse_session_target

# QQ 系统表情表：QSid → 中文名（微笑/捂脸/…）。
# 表从 LLBot 自带的 face_config.sysface 提取（见 app/face_names.json），
# Satori 的 <face id="..."/> 只带 id，不带名字，所以在这里补。
try:
    with open(Path(__file__).with_name("face_names.json"), "r", encoding="utf-8") as _fh:
        _FACE_NAMES: dict[str, str] = json.load(_fh)
except Exception:  # 表缺失也不能影响收消息
    _FACE_NAMES = {}


def _session_key(event: MessageEvent) -> tuple[str, bool]:
    """返回 (会话 key, 是否群聊)。

    私聊: qq_{user_id}
    群聊: qq_g_{channel_id}
    """
    if event.channel.type == ChannelType.DIRECT:
        return f"qq_{event.user.id}", False
    return f"qq_g_{event.channel.id}", True


def parse_session_target(session_id: str) -> tuple[str, str] | None:
    """把固定的会话 id 解析成推送目标：("private", QQ号) / ("group", 群号)。

    实现在 `app/session_target.py`（纯函数、不依赖 satori，便于单测）；这里重新导出，
    保持 `from app.qq_bridge import parse_session_target` 可用。
    """
    return _parse_session_target(session_id)


def _extract_element_id(elem, attrs: dict) -> str:
    """从元素上取 id。

    不同来源的元素形状不一样（typed 类、通用 Element、客户端解析的原始对象），
    attrs 里不一定有 `id`；兜底从对象属性、以及未渲染的标签文本里抠，
    避免退化成没有信息量的 `[表情]`。
    """
    for key in ("id", "face_id", "faceId", "emoji_id", "emojiId"):
        value = (attrs or {}).get(key)
        if value not in (None, ""):
            return str(value).strip()

    for attr in ("id", "face_id", "faceId", "emoji_id", "emojiId", "index", "faceIndex"):
        value = getattr(elem, attr, None)
        if value not in (None, ""):
            return str(value).strip()

    # 最后兜底：通用 Element 的 str() 是 `<face id="264" .../>` 这种标签
    try:
        match = re.search(r'\bid\s*=\s*"([^"]+)"', str(elem))
        if match:
            return match.group(1).strip()
    except Exception:
        pass
    return ""

_HTML_IMG_RE = re.compile(r"<img\b[^>]*?src\s*=\s*[\"']?([^\"'\s>]+)[\"']?[^>]*>", re.IGNORECASE)


def _html_img_srcs(text: str) -> list[str]:
    """从纯文本里抠出内嵌的 <img src="…">（QQ 图文消息/合并转发的常见形态）。"""
    if not text or "<img" not in text.lower():
        return []
    return [src for src in _HTML_IMG_RE.findall(text) if src]


def _strip_html_img(text: str) -> str:
    """去掉内嵌 <img> 标签，避免把 HTML 当正文喂给模型。"""
    if not text or "<img" not in text.lower():
        return text or ""
    return _HTML_IMG_RE.sub("", text).strip()


def _render_element(elem) -> str:
    """把 Satori 消息元素渲染成给模型看的纯文本。

    为什么不能直接用 `str(elem)`：
    satori 的 `ELEMENT_TYPE_MAP` 只认识 text/at/emoji/sharp/image/audio/video/file/author，
    QQ 原生表情 `<face id="264" type="2" platform="llonebot"/>` 会被解析成通用
    `Element(type="face")`，而它的 `__str__` 会把元素 **dumps 回 XML 原文** ——
    于是提示词和历史里就多了一段形如 `<face id="264" .../>` 的标签，
    模型容易把它当成标签/指令，长期污染上下文。
    """
    if isinstance(elem, Text):
        # 图文消息的图片可能就在文本里（<img src="…">），正文只留文字
        return _strip_html_img(str(elem))

    kind = str(getattr(elem, "type", "") or "").strip().lower()
    if kind in ("", "text"):
        # satori 有两种表示：typed 的 Text 类，以及解析器产出的通用
        # Element(type="text")。后者的 str() 就是正文（没有标签）。
        # 但 QQ 的图文消息会把图片塞成正文里的 `<img src="…">`：
        # 这里只留文字，图片由 _extract_images 单独抽（见下）。
        return _strip_html_img(str(elem))
    attrs = getattr(elem, "attrs", {}) or {}

    if kind in ("face", "mface", "marketface", "emoji"):
        fid = _extract_element_id(elem, attrs)
        if not fid:
            return "[表情]"
        if kind == "emoji":
            # satori 的 emoji 元素 id 是 Unicode 码点（如 128512 = 😀）
            try:
                return chr(int(fid))
            except Exception:
                return "[表情]"
        name = _FACE_NAMES.get(fid, "")
        return f"[表情:{name}]" if name else f"[表情:{fid}]"
    if kind in ("at",):
        # @ 目标（通常是机器人自己）不喂给模型，避免"被点名的错觉"
        name = str(attrs.get("name") or "").strip()
        if not name or name in ("all", "全体成员"):
            return ""
        return f"@{name}"
    if kind in ("quote", "reply"):
        # 引用回复：被引用的原文另外会在消息里出现，这里不重复
        return ""
    if kind in ("image", "img", "file", "audio", "video", "sharp"):
        return ""   # 这些走 images / files 专用通道
    if kind in ("forward", "multimsg", "mutilmsg", "json", "ark", "lightapp"):
        # 合并转发 / 卡片：satori 不给节点内容，只能留标记（模型据此知道"这里有一段被转发的历史"）
        return "[转发内容]"
    return f"[{kind}]" if kind else ""


def _strip_at(elements) -> str:
    """把消息元素转成纯文本（图片/文件/引用等资源元素只留占位或丢弃）。"""
    return "".join(_render_element(elem) for elem in elements).strip()


def _extract_images(elements) -> list[str]:
    """提取消息中的图片 URL。

    两种来源都要收：
    1. typed `Image` 元素（Satori 标准）；
    2. 文本里内嵌的 `<img src="…">`（QQ 图文消息 / 合并转发的常见形态）。
    漏掉第 2 种会让"带图消息"的 images 为空，调度器的 3000ms 合并窗口也就不会生效。
    """
    urls: list[str] = []
    for elem in elements:
        if isinstance(elem, Image):
            src = getattr(elem, "src", "") or ""
            if src and src not in urls:
                urls.append(src)
            continue
        kind = str(getattr(elem, "type", "") or "").strip().lower()
        if kind in ("", "text"):
            for src in _html_img_srcs(str(elem)):
                if src not in urls:
                    urls.append(src)
    return urls


def _extract_files(elements) -> list[tuple[str, str]]:
    """提取消息中的文件元素，返回 [(src, name), ...]（Satori File 的 src/title 字段）。"""
    files = []
    for elem in elements:
        if isinstance(elem, File):
            src = getattr(elem, "src", "") or ""
            name = getattr(elem, "title", "") or ""
            if src:
                files.append((src, name))
    return files


def _safe_basename(name: str) -> str:
    """清洗文件名：只保留 basename，防止路径穿越。"""
    raw = (name or "").replace("\\", "/").strip()
    if not raw:
        return "file"
    base = raw.rsplit("/", 1)[-1]
    base = re.sub(r"[^\w.\u4e00-\u9fff-]+", "_", base)
    base = base.strip("._")
    return base or "file"


def _at_me(account: Account, elements) -> bool:
    """判断消息是否明确 @ 了机器人（@全体/@here 不视为 @ 机器人）。"""
    bot_id = account.self_id
    for elem in elements:
        if not isinstance(elem, At):
            continue
        if elem.id == bot_id:
            return True
    return False


class QQBridge:
    def __init__(self):
        self._app = App(WebsocketsInfo(
            host=config.SATORI_HOST,
            port=config.SATORI_PORT,
            token=config.SATORI_TOKEN,
        ))
        self._main_loop: asyncio.AbstractEventLoop | None = None
        self._sessions: dict[str, tuple[Account, MessageEvent]] = {}
        self._on_message: Callable[[str, str, str, list[str], list[tuple[str, bytes]]], Awaitable[None]] | None = None

    def on_qq_message(self, handler: Callable[[str, str, str, list[str], list[tuple[str, bytes]]], Awaitable[None]]):
        """注册 QQ 消息回调：handler(user_id, content, session_id, image_urls, files)
        files 为已下载的 [(文件名, 字节内容), ...]"""
        self._on_message = handler

    @staticmethod
    def _fix_host(url: str) -> str:
        """把 127.0.0.1/localhost 的图片地址改写为 satori 实际可达地址。"""
        parsed = urlparse(url)
        if parsed.hostname in {"127.0.0.1", "localhost", "0.0.0.0"}:
            port = parsed.port or config.SATORI_PORT
            netloc = f"{config.SATORI_HOST}:{port}"
            url = urlunparse(parsed._replace(netloc=netloc))
        return url

    async def resolve_images(self, urls: list[str]) -> list[str]:
        """把图片 URL 抓取为 base64 data URL，供模型多模态输入使用。"""
        if not urls:
            return []
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            out = []
            for url in urls:
                out.append(await self._resolve_one(session, url))
            return out

    async def download_files(self, file_specs: list[tuple[str, str]]) -> list[tuple[str, bytes]]:
        """下载文件列表，返回 [(清洗后的文件名, 字节内容), ...]。

        超过 MAX_FILE_BYTES 的文件跳过并打印警告。
        """
        if not file_specs:
            return []
        timeout = aiohttp.ClientTimeout(total=60)
        out: list[tuple[str, bytes]] = []
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for src, name in file_specs:
                safe_name = _safe_basename(name)
                data = await self._fetch_bytes(session, self._fix_host(src))
                if not data:
                    print(f"[qq-llbot] 文件下载失败: {src}", flush=True)
                    continue
                if len(data) > config.MAX_FILE_BYTES:
                    print(
                        f"[qq-llbot] 文件过大已跳过: {safe_name} {len(data)} > {config.MAX_FILE_BYTES}",
                        flush=True,
                    )
                    continue
                out.append((safe_name, data))
        return out

    async def _fetch_bytes(self, session: aiohttp.ClientSession, url: str) -> bytes:
        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    print(f"[qq-llbot] 下载失败 status={resp.status} url={url}", flush=True)
                    return b""
                return await resp.read()
        except Exception as e:
            print(f"[qq-llbot] 下载失败 {url}: {e}", flush=True)
            return b""

    async def _resolve_one(self, session: aiohttp.ClientSession, url: str) -> str:
        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    print(f"[qq-llbot] 图片下载失败 status={resp.status} url={url}", flush=True)
                    return ""
                data = await resp.read()
                if not data:
                    return ""
                ctype = resp.headers.get("Content-Type", "")
                if not ctype or not ctype.startswith("image/"):
                    ctype = mimetypes.guess_type(url.split("?", 1)[0])[0] or "image/jpeg"
                b64 = base64.b64encode(data).decode("ascii")
                return f"data:{ctype};base64,{b64}"
        except Exception as e:
            print(f"[qq-llbot] 图片下载失败 {url}: {e}", flush=True)
            return ""

    @property
    def app(self) -> App:
        return self._app

    @property
    def sessions(self) -> dict:
        return self._sessions

    def setup_listener(self):
        """注册 Satori 消息监听"""

        @self._app.register_on(EventType.MESSAGE_CREATED)
        async def _on_qq_message(account: Account, event: MessageEvent):
            user_id = event.user.id
            elements = event.message.message
            session_key, is_group = _session_key(event)

            # 群聊：只有被 @ 机器人才继续下发
            if is_group and config.GROUP_AT_REQUIRED and not _at_me(account, elements):
                print(
                    f"[qq-llbot] 群聊消息未@机器人，忽略 user={user_id} group={event.channel.id}",
                    flush=True,
                )
                return

            content = _strip_at(elements)
            image_urls = _extract_images(elements)
            file_specs = _extract_files(elements)

            # 保存 session，用于后续推送回复
            self._sessions[session_key] = (account, event)

            print(
                f"[qq-llbot] 收到 QQ 消息 user={user_id} session={session_key} "
                f"content={content[:50]}... images={len(image_urls)} files={len(file_specs)}",
                flush=True,
            )

            if self._on_message and (content or image_urls or file_specs):
                # 下载文件（顺序执行，失败/超大的会跳过）
                files = await self.download_files(file_specs)
                if len(files) != len(file_specs):
                    print(
                        f"[qq-llbot] 部分文件下载失败: 成功 {len(files)}/{len(file_specs)}",
                        flush=True,
                    )
                await self._on_message(user_id, content, session_key, image_urls, files)

    async def send(
        self,
        session_id: str,
        text: str = "",
        images: list[str] | None = None,
        files: list[tuple[str, bytes]] | None = None,
    ) -> bool:
        """向 QQ 会话（私聊用户/群）推送消息。返回是否**真的送达**。

        发送目标优先用 `_sessions` 里缓存的 event（能带上 `referrer`，表现为引用回复）；
        缓存未命中时**从 session_id 重建目标**（主动推送 / 重启后 / 缓冲补发都走这条）。
        两条路径的差别只有引用回复样式 —— 对定时提醒、离线转投这类本就没有入站消息的
        推送，`referrer=None` 是唯一可能，也是语义正确的。

        返回 bool 是投递确认的前提：调用方据此 ack，调度器才知道该转投还是缓冲。

        images: 图片 URL 列表
        files:  [(文件名, 字节内容), ...] 以 Satori File 元素发送
        """
        msg = []
        if text:
            msg.append(Text(text))

        for url in (images or []):
            try:
                msg.append(Image(src=url))
            except Exception as e:
                print(f"[qq-llbot] 图片加载失败: {url}, {e}", flush=True)

        for name, data in (files or []):
            try:
                msg.append(File.of(raw=data, name=name))
            except Exception as e:
                print(f"[qq-llbot] 文件构造失败: {name}, {e}", flush=True)
                # 降级：至少告知文件名，避免静默丢失
                msg.append(Text(f"[文件] {name}"))

        if not msg:
            return False

        # 路径 1：本轮刚收到过消息 → 用缓存 event（保引用回复）
        cached = self._sessions.get(session_id)
        if cached is not None:
            account, event = cached
            try:
                await account.send(event, msg)
                print(f"[qq-llbot] 已推送消息给 {session_id}", flush=True)
                return True
            except Exception as e:
                print(f"[qq-llbot] 推送失败（缓存 event 路径）session={session_id}: {e}", flush=True)
                return False

        # 路径 2：从 session_id 重建目标（主动推送 / 重启后 / 缓冲补发）
        target = parse_session_target(session_id)
        if target is None:
            print(f"[qq-llbot] 无法解析会话目标，放弃推送 session={session_id!r}", flush=True)
            return False

        account = self._pick_account()
        if account is None:
            print(
                f"[qq-llbot] 当前无可用 QQ 账号（Satori 未登录），无法推送 session={session_id}",
                flush=True,
            )
            return False

        kind, target_id = target
        try:
            if kind == "group":
                # 群号就是 channel id
                await account.send_message(target_id, msg)
            else:
                # 私聊：先拿 channel 再发。
                # 注意不能用 send_private_message —— 它内部是 "".join(str(i) for i in message)，
                # 会把元素列表拍平成纯文本，图片/文件语义全丢。
                channel = await account.user_channel_create(user_id=target_id)
                await account.send_message(channel, msg)
            print(f"[qq-llbot] 已推送消息给 {session_id}（重建目标路径）", flush=True)
            return True
        except Exception as e:
            print(f"[qq-llbot] 推送失败（重建目标路径）session={session_id}: {e}", flush=True)
            return False

    def _pick_account(self) -> Account | None:
        """取一个已登录的 Satori 账号（App.accounts 在 WebSocket 登录时就填好了，
        不需要任何用户消息）。优先取 `connected` 已就绪的。"""
        accounts = list(getattr(self._app, "accounts", {}).values())
        for account in accounts:
            connected = getattr(account, "connected", None)
            if connected is not None and connected.is_set():
                return account
        return accounts[0] if accounts else None

    def is_qq_online(self) -> bool:
        """QQ 渠道是否可用（Satori 账号已连接）。"""
        account = self._pick_account()
        if account is None:
            return False
        connected = getattr(account, "connected", None)
        return True if connected is None else connected.is_set()

    def setup_lifecycle(self, on_online, on_offline=None):
        """注册 Satori 登录状态回调。

        上线 → 通知调度器补发缓冲（原有行为）。
        下线 → 交给调用方**主动退订**调度器事件流：这样调度器会按优先级把可见事件
        交给另一个后端（qqofficial），而不是继续往一个发不出去的通道空投。
        全程不用心跳：Satori 的状态机事件就是边沿。
        """

        @self._app.lifecycle
        async def _on_login_state(account: Account, state):
            try:
                is_online = str(getattr(state, "name", state)).upper().endswith("ONLINE")
            except Exception:
                is_online = False
            if is_online:
                print("[qq-llbot] Satori 已上线，通知调度器补发缓冲", flush=True)
                await on_online()
                return
            print(f"[qq-llbot] Satori 状态变化 -> {state!r}（非在线）", flush=True)
            if on_offline is not None:
                await on_offline()


    async def run(self):
        """启动 Satori 客户端"""
        self._main_loop = asyncio.get_running_loop()
        await self._app.run_async()

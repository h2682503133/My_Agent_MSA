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
import mimetypes
from typing import Callable, Awaitable
from urllib.parse import urlparse, urlunparse

import aiohttp
from satori.client import App, WebsocketsInfo, Account
from satori.const import EventType
from satori.event import MessageEvent
from satori.element import At, Text, Image
from satori.model import ChannelType

from app import config


def _session_key(event: MessageEvent) -> tuple[str, bool]:
    """返回 (会话 key, 是否群聊)。

    私聊: qq_{user_id}
    群聊: qq_g_{channel_id}
    """
    if event.channel.type == ChannelType.DIRECT:
        return f"qq_{event.user.id}", False
    return f"qq_g_{event.channel.id}", True


def _strip_at(elements) -> str:
    """去掉 @ 与图片元素，返回纯文本内容（其余元素原样保留）。"""
    parts = []
    for elem in elements:
        if isinstance(elem, (At, Image)):
            continue
        parts.append(str(elem))
    return "".join(parts).strip()


def _extract_images(elements) -> list[str]:
    """提取消息中的图片元素 src 列表（Satori Image 的 src 字段）。"""
    urls = []
    for elem in elements:
        if isinstance(elem, Image):
            src = getattr(elem, "src", "") or ""
            if src:
                urls.append(src)
    return urls


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
        self._on_message: Callable[[str, str, str, list[str]], Awaitable[None]] | None = None

    def on_qq_message(self, handler: Callable[[str, str, str, list[str]], Awaitable[None]]):
        """注册 QQ 消息回调：handler(user_id, content, session_id, image_urls)"""
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

    async def _resolve_one(self, session: aiohttp.ClientSession, url: str) -> str:
        url = self._fix_host(url)
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

            # 保存 session，用于后续推送回复
            self._sessions[session_key] = (account, event)

            print(
                f"[qq-llbot] 收到 QQ 消息 user={user_id} session={session_key} "
                f"content={content[:50]}...",
                flush=True,
            )

            if self._on_message and (content or image_urls):
                await self._on_message(user_id, content, session_key, image_urls)

    async def send(self, session_id: str, text: str = "", images: list[str] | None = None):
        """向 QQ 会话（私聊用户/群）推送消息"""
        if session_id not in self._sessions:
            print(f"[qq-llbot] 会话 {session_id} 不在线，无法推送", flush=True)
            return

        account, event = self._sessions[session_id]
        msg = []
        if text:
            msg.append(Text(text))

        for url in (images or []):
            try:
                msg.append(Image(src=url))
            except Exception as e:
                print(f"[qq-llbot] 图片加载失败: {url}, {e}", flush=True)

        if not msg:
            return

        try:
            await account.send(event, msg)
            print(f"[qq-llbot] 已推送消息给 {session_id}", flush=True)
        except Exception as e:
            print(f"[qq-llbot] 推送失败 session={session_id}: {e}", flush=True)

    async def run(self):
        """启动 Satori 客户端"""
        self._main_loop = asyncio.get_running_loop()
        await self._app.run_async()

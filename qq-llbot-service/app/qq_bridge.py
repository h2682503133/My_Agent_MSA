"""
QQ LLBot 桥接：通过 Satori 协议收发 QQ 消息。

参考旧架构 core/gateway/qq_bridge.py 实现，适配微服务架构：
- 接收 QQ 消息 → 调用 scheduler.CreateTask(channel="qq")
- 订阅 scheduler 事件 → 收到 assistant_message → 推送回 QQ
"""
import asyncio
from typing import Callable, Awaitable

from satori.client import App, WebsocketsInfo, Account
from satori.const import EventType
from satori.event import MessageEvent
from satori.element import Text, Image

from app import config


class QQBridge:
    def __init__(self):
        self._app = App(WebsocketsInfo(
            host=config.SATORI_HOST,
            port=config.SATORI_PORT,
            token=config.SATORI_TOKEN,
        ))
        self._main_loop: asyncio.AbstractEventLoop | None = None
        self._sessions: dict[str, tuple[Account, MessageEvent]] = {}
        self._on_message: Callable[[str, str], Awaitable[None]] | None = None

    def on_qq_message(self, handler: Callable[[str, str], Awaitable[None]]):
        """注册 QQ 消息回调：handler(user_id, content)"""
        self._on_message = handler

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
            content = event.message.content.strip()

            # 保存 session，用于后续推送回复
            self._sessions[user_id] = (account, event)

            print(f"[qq-llbot] 收到 QQ 消息 user={user_id} content={content[:50]}...", flush=True)

            if self._on_message and content:
                await self._on_message(user_id, content)

    async def send(self, user_id: str, text: str = "", images: list[str] | None = None):
        """向 QQ 用户推送消息"""
        if user_id not in self._sessions:
            print(f"[qq-llbot] 用户 {user_id} 不在线，无法推送", flush=True)
            return

        account, event = self._sessions[user_id]
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
            print(f"[qq-llbot] 已推送消息给 {user_id}", flush=True)
        except Exception as e:
            print(f"[qq-llbot] 推送失败 user={user_id}: {e}", flush=True)

    async def run(self):
        """启动 Satori 客户端"""
        self._main_loop = asyncio.get_running_loop()
        await self._app.run_async()

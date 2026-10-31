"""OneBot 11 桥接：正向 / 反向 WebSocket，收发消息。

- 正向（forward）：我们连对方 WS，收事件 + 调 action；
- 反向（reverse）：对方连我们，收事件 + 在同一个连接上调 action；
- 两种都支持 token 鉴权（Authorization: Bearer / ?access_token=）；
- 断线自动重连（指数退避，封顶 ONEBOT_RECONNECT_MAX）。

协议细节见 app/onebot_protocol.py（纯函数，可离线单测）。
"""

from __future__ import annotations

import asyncio
import json
import time
import urllib.parse
from typing import Awaitable, Callable

import websockets

from app import config
from app.onebot_protocol import (
    ParsedMessage,
    authorized,
    build_send_payload,
    parse_event,
    should_respond,
)

MessageHandler = Callable[[ParsedMessage], Awaitable[None]]


class OneBotBridge:
    def __init__(self) -> None:
        self._handler: MessageHandler | None = None
        self._on_online: Callable[[], Awaitable[None]] | None = None
        self._on_offline: Callable[[], Awaitable[None]] | None = None

        self._online = False
        self._self_id = config.ONEBOT_SELF_ID
        self._api_futures: dict[str, asyncio.Future] = {}
        self._send_lock = asyncio.Lock()
        # 当前可用的连接（正向的连接 / 反向的被连接）
        self._conn: websockets.WebSocketServerProtocol | None = None
        self._echo_seq = 0

    # ── 回调注册 ────────────────────────────────────────────
    def on_qq_message(self, handler: MessageHandler) -> None:
        self._handler = handler

    def setup_lifecycle(
        self,
        on_online: Callable[[], Awaitable[None]],
        on_offline: Callable[[], Awaitable[None]],
    ) -> None:
        self._on_online = on_online
        self._on_offline = on_offline

    def is_qq_online(self) -> bool:
        return self._online and self._conn is not None

    # ── 生命周期 ────────────────────────────────────────────
    async def _set_online(self, ws) -> None:
        self._conn = ws
        if not self._online:
            self._online = True
            print("[qq-onebot] QQ 已上线", flush=True)
            if self._on_online:
                await self._on_online()

    async def _set_offline(self) -> None:
        was_online = self._online
        self._online = False
        self._conn = None
        for future in list(self._api_futures.values()):
            if not future.done():
                future.set_exception(RuntimeError("onebot 连接已断开"))
        self._api_futures.clear()
        if was_online:
            print("[qq-onebot] QQ 已离线", flush=True)
            if self._on_offline:
                await self._on_offline()

    # ── 事件处理 ────────────────────────────────────────────
    async def _handle_frame(self, raw: str | bytes) -> None:
        try:
            data = json.loads(raw)
        except Exception:  # noqa: BLE001
            return
        if not isinstance(data, dict):
            return

        # action 的响应（带 echo）
        echo = data.get("echo")
        if echo is not None and str(echo) in self._api_futures:
            future = self._api_futures.pop(str(echo), None)
            if future and not future.done():
                future.set_result(data)
            return

        # 从事件里学习 self_id（reverse 模式下我们不知道账号也能跑）
        if not self._self_id:
            candidate = str(data.get("self_id") or "")
            if candidate:
                self._self_id = candidate
                print(f"[qq-onebot] 学到机器人账号 self_id={candidate}", flush=True)

        parsed = parse_event(data, self._self_id)
        if parsed is None:
            return
        if not should_respond(parsed, config.ONEBOT_GROUP_REQUIRE_AT):
            print(
                f"[qq-onebot] 忽略未 @ 的群消息 session={parsed.session_id} "
                f"user={parsed.user_id}",
                flush=True,
            )
            return
        if not parsed.text and not parsed.images and not parsed.files:
            return
        if self._handler:
            await self._handler(parsed)

    async def _recv_loop(self, ws) -> None:
        try:
            async for raw in ws:
                await self._handle_frame(raw)
        except websockets.ConnectionClosed:
            pass
        except Exception as exc:  # noqa: BLE001
            print(f"[qq-onebot] 接收循环异常：{exc}", flush=True)

    # ── 发送 ────────────────────────────────────────────────
    async def call_api(self, action: str, params: dict, timeout: float | None = None) -> dict:
        """调 OneBot action 接口；返回响应 dict（ok/retcode/data）。"""
        ws = self._conn
        if ws is None:
            raise RuntimeError("onebot 未连接")
        self._echo_seq += 1
        echo = f"echo-{self._echo_seq}-{int(time.time() * 1000)}"
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._api_futures[echo] = future
        payload = {"action": action, "params": params, "echo": echo}
        async with self._send_lock:
            await ws.send(json.dumps(payload, ensure_ascii=False))
        try:
            return await asyncio.wait_for(future, timeout=timeout or config.ONEBOT_API_TIMEOUT)
        finally:
            self._api_futures.pop(echo, None)

    async def send(self, session_id: str, text: str = "", images: list[str] | None = None) -> bool:
        """把文本/图片推给某个会话；返回是否成功（失败由上层决定退订/重试）。"""
        try:
            action, params = build_send_payload(session_id, text=text, images=images)
        except ValueError as exc:
            print(f"[qq-onebot] 发送失败：{exc}", flush=True)
            return False
        try:
            response = await self.call_api(action, params)
        except Exception as exc:  # noqa: BLE001
            print(f"[qq-onebot] 发送异常 session={session_id}: {exc}", flush=True)
            return False
        ok = str(response.get("status") or "").lower() == "ok" or bool(response.get("retcode") == 0)
        if not ok:
            print(f"[qq-onebot] 发送被拒 session={session_id}: {str(response)[:200]}", flush=True)
        return ok

    # ── 连接方式 ────────────────────────────────────────────
    async def _forward_loop(self) -> None:
        attempt = 0
        while True:
            headers = {}
            if config.ONEBOT_TOKEN:
                headers["Authorization"] = f"Bearer {config.ONEBOT_TOKEN}"
            try:
                async with websockets.connect(
                    config.ONEBOT_WS_URL,
                    additional_headers=headers,
                    max_size=16 * 1024 * 1024,
                    open_timeout=10,
                ) as ws:
                    attempt = 0
                    await self._set_online(ws)
                    await self._recv_loop(ws)
            except Exception as exc:  # noqa: BLE001
                attempt += 1
                delay = min(config.ONEBOT_RECONNECT_MAX, 2 ** min(attempt, 5))
                print(f"[qq-onebot] 连接 {config.ONEBOT_WS_URL} 失败（{exc}），{delay:.0f}s 后重试", flush=True)
                await self._set_offline()
                await asyncio.sleep(delay)

    async def _reverse_loop(self) -> None:
        host, _, port_text = config.ONEBOT_WS_LISTEN.partition(":")
        port = int(port_text or "6700")

        async def handler(ws) -> None:
            # 鉴权：NapCat 会把 token 放 Authorization 或 ?access_token=
            path = getattr(getattr(ws, "request", None), "path", "") or ""
            query = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(path).query))
            headers = dict(getattr(getattr(ws, "request", None), "headers", {}) or {})
            if not authorized(headers, query, config.ONEBOT_TOKEN):
                print("[qq-onebot] 反向连接鉴权失败，关闭", flush=True)
                await ws.close(code=1008, reason="unauthorized")
                return
            print("[qq-onebot] 对方已连入（反向 WS）", flush=True)
            await self._set_online(ws)
            try:
                await self._recv_loop(ws)
            finally:
                await self._set_offline()
                print("[qq-onebot] 反向连接已断开", flush=True)

        print(f"[qq-onebot] 反向 WS 监听 {host}:{port}", flush=True)
        async with websockets.serve(handler, host, port, max_size=16 * 1024 * 1024):
            await asyncio.Future()   # 永久运行

    async def run(self) -> None:
        mode = config.ONEBOT_MODE
        if mode == "reverse":
            await self._reverse_loop()
        elif mode == "auto":
            # 先试正向；连不上就开反向监听
            try:
                async with websockets.connect(
                    config.ONEBOT_WS_URL,
                    additional_headers=({"Authorization": f"Bearer {config.ONEBOT_TOKEN}"}
                                        if config.ONEBOT_TOKEN else {}),
                    open_timeout=5,
                    max_size=16 * 1024 * 1024,
                ) as ws:
                    await self._set_online(ws)
                    await self._recv_loop(ws)
                    return
            except Exception as exc:  # noqa: BLE001
                print(f"[qq-onebot] auto：正向连接失败（{exc}），转反向监听", flush=True)
            await self._reverse_loop()
        else:
            await self._forward_loop()

"""QQ 官方 WebSocket 网关客户端。

流程（官方文档 /dev-prepare/event-emit/websocket.html）：
  1. GET /gateway/bot 拿 wss 地址（含建议分片数）
  2. 连上后收 op=10 Hello { heartbeat_interval }
  3. 鉴权 op=2 Identify { token:"QQBot {token}", intents, shard, properties }
     —— 已有 session_id+seq 时改为 op=6 Resume（会自动补发遗漏事件）
  4. 按周期发 op=1 心跳（d = 收到的最新 s），收到 op=11 为 ACK
  5. op=0 Dispatch 携带 t/d/s；t=READY 表示鉴权成功
  6. 断线后先尝试 Resume，失败则重新 Identify

设计取舍：本模块只管"可靠收事件 + 重连"，事件语义交给 bridge。
"""
import asyncio
import json

import aiohttp


class _Reconnect(Exception):
    """需要重建连接（Resume 优先）。"""


class _InvalidSession(Exception):
    """session 不可恢复，必须重新 Identify。"""


class GatewayClient:
    def __init__(
        self,
        gateway_getter,
        token_getter,
        intents: int,
        on_dispatch,
        on_ready=None,
    ):
        self.gateway_getter = gateway_getter
        self.token_getter = token_getter
        self.intents = intents
        self.on_dispatch = on_dispatch
        self.on_ready = on_ready
        self.session_id = ""
        self.seq = 0
        self._closed = False

    async def run(self):
        backoff = 1.0
        while not self._closed:
            try:
                await self._connect_once()
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except _InvalidSession as exc:
                print(f"[qq-official] session 失效，重新鉴权: {exc}", flush=True)
                self.session_id = ""
                self.seq = 0
            except _Reconnect as exc:
                print(f"[qq-official] 网关要求重连: {exc}", flush=True)
            except Exception as exc:
                print(f"[qq-official] websocket 断开: {type(exc).__name__} {exc}", flush=True)
            if self._closed:
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    async def close(self):
        self._closed = True

    # ── 单次连接 ────────────────────────────────────────────
    async def _connect_once(self):
        url, _shards = await self.gateway_getter()
        if not url:
            raise RuntimeError("网关地址为空（token 或 appid 无效？）")
        token = await self.token_getter()
        if not token:
            raise RuntimeError("access_token 为空")

        async with aiohttp.ClientSession() as http:
            async with http.ws_connect(url, heartbeat=None, max_msg_size=0) as ws:
                hello = await asyncio.wait_for(ws.receive(), timeout=15)
                if hello.type != aiohttp.WSMsgType.TEXT:
                    raise RuntimeError("首个下行业务帧不是文本 Hello")
                payload = json.loads(hello.data)
                if payload.get("op") != 10:
                    raise RuntimeError(f"期望 op=10 Hello，实得 {payload.get('op')}")
                interval_ms = float((payload.get("d") or {}).get("heartbeat_interval") or 45000)
                interval = max(interval_ms / 1000.0, 5.0)

                resuming = bool(self.session_id and self.seq)
                if resuming:
                    await ws.send_json({
                        "op": 6,
                        "d": {
                            "token": f"QQBot {token}",
                            "session_id": self.session_id,
                            "seq": self.seq,
                        },
                    })
                else:
                    await ws.send_json({
                        "op": 2,
                        "d": {
                            "token": f"QQBot {token}",
                            "intents": self.intents,
                            "shard": [0, 1],
                            "properties": {
                                "$os": "linux",
                                "$browser": "my-agent-msa",
                                "$device": "qq-official-service",
                            },
                        },
                    })

                acked = {"seq_at_last_beat": self.seq, "missed": 0}
                beat = asyncio.create_task(self._heartbeat(ws, interval, acked))
                try:
                    async for msg in ws:
                        if msg.type is aiohttp.WSMsgType.TEXT:
                            await self._handle(json.loads(msg.data), acked)
                        elif msg.type in (
                            aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.CLOSING,
                            aiohttp.WSMsgType.ERROR,
                        ):
                            raise _Reconnect("连接关闭")
                finally:
                    beat.cancel()
                    try:
                        await beat
                    except (asyncio.CancelledError, Exception):
                        pass

    async def _heartbeat(self, ws, interval, acked):
        while True:
            await asyncio.sleep(interval)
            acked["missed"] += 1
            # 连续两个周期没有 ACK 视为死连接
            if acked["missed"] >= 2:
                print("[qq-official] 心跳连续未 ACK，主动重连", flush=True)
                await ws.close()
                return
            try:
                await ws.send_json({"op": 1, "d": self.seq or None})
            except Exception:
                return

    async def _handle(self, payload: dict, acked: dict):
        op = payload.get("op")
        data = payload.get("d")
        if op == 11:  # Heartbeat ACK
            acked["missed"] = 0
            return
        if op == 7:  # Reconnect
            raise _Reconnect("op=7 reconnect")
        if op == 9:  # Invalid Session
            raise _InvalidSession("op=9 invalid session")
        if op != 0:
            return

        seq = payload.get("s")
        if isinstance(seq, int) and seq > 0:
            self.seq = seq

        t = str(payload.get("t") or "")
        if t == "READY":
            d = data if isinstance(data, dict) else {}
            self.session_id = str(d.get("session_id") or "")
            print(
                f"[qq-official] 鉴权成功 session={self.session_id} "
                f"bot={(d.get('user') or {}).get('username', '')}",
                flush=True,
            )
            if self.on_ready is not None:
                await self.on_ready()
            return
        if t == "RESUMED":
            print("[qq-official] resume 成功，补发事件已接收", flush=True)
            return
        if not t:
            return
        if self.on_dispatch is not None:
            await self.on_dispatch(t, data if isinstance(data, dict) else {})

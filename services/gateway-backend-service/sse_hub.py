import asyncio
import json
import os
import time
from collections import defaultdict
from typing import AsyncIterator, Dict, List, Set

from schemas import TaskEvent

MAX_AGENT_BUFFER = 100
# 用户离线时暂存的"可见消息"条数上限（超出丢弃最旧）。离线兜底缓存，
# 用户重新建立 SSE 连接时按原顺序回放补发。
MAX_OFFLINE_PER_USER = int(os.getenv("OFFLINE_BUFFER_MAX", "50"))
# 离线消息保留时长（秒）；0 = 不按时间过期，只按条数上限淘汰
OFFLINE_TTL_SECONDS = int(os.getenv("OFFLINE_BUFFER_TTL", "0"))


class SSEHub:
    def __init__(self) -> None:
        self._queues: Dict[str, Set[asyncio.Queue]] = defaultdict(set)
        self._lock = asyncio.Lock()
        # 跨 agent buffer: (user_id, agent_id) -> [payload]
        self._agent_buffer: Dict[str, List[dict]] = defaultdict(list)
        # 离线缓冲（用户无任何在线 SSE 连接时，可见消息暂存在这里等上线回放）
        # user_id -> [(ts, payload), ...]
        self._offline: Dict[str, List[tuple[float, dict]]] = {}
        # 轻量计数，供 /api/debug/sse_state 观测
        self._counters = {
            "delivered": 0,          # 成功投递给在线连接的事件数
            "offline_held": 0,       # 存入离线缓冲（等上线回放）
            "offline_replayed": 0,   # 上线时回放补发的条数
            "offline_evicted": 0,    # 因超条数/超时被淘汰的条数
            "forwarded_other": 0,    # 离线时转投其它渠道成功的条数
            "no_target": 0,          # 离线且无其它渠道可用（计入离线缓冲）
        }

    def _buffer_key(self, user_id: str, agent_id: str) -> str:
        return f"{user_id}::{agent_id}"

    def _prune_offline(self, user_id: str) -> None:
        """按 TTL 清理该用户过期的离线消息（调用方需持锁）。"""
        if OFFLINE_TTL_SECONDS <= 0:
            return
        buf = self._offline.get(user_id)
        if not buf:
            return
        deadline = time.time() - OFFLINE_TTL_SECONDS
        kept = [(ts, payload) for ts, payload in buf if ts >= deadline]
        evicted = len(buf) - len(kept)
        if evicted:
            self._counters["offline_evicted"] += evicted
        if kept:
            self._offline[user_id] = kept
        else:
            self._offline.pop(user_id, None)

    async def subscribe(self, user_id: str, agent_id: str | None = None) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=200)
        async with self._lock:
            self._queues[user_id].add(queue)
            # 回放当前 agent 的跨 agent buffer
            if agent_id:
                key = self._buffer_key(user_id, agent_id)
                agent_buf = self._agent_buffer.pop(key, [])
            else:
                agent_buf = []
            # 回放离线缓冲（用户离线期间积压的可见消息，按原顺序补发）
            self._prune_offline(user_id)
            offline_buf = self._offline.pop(user_id, [])
            self._counters["offline_replayed"] += len(offline_buf)
        for payload in agent_buf:
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                break
        for _ts, payload in offline_buf:
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                break
        return queue

    async def unsubscribe(self, user_id: str, queue: asyncio.Queue) -> None:
        async with self._lock:
            queues = self._queues.get(user_id)
            if not queues:
                return
            queues.discard(queue)
            if not queues:
                self._queues.pop(user_id, None)

    async def has_online(self, user_id: str) -> bool:
        """该用户当前是否有在线 SSE 连接（离线判定用）。"""
        if not user_id:
            return False
        async with self._lock:
            return bool(self._queues.get(user_id))

    async def publish(self, event: TaskEvent) -> bool:
        """投递给该用户的在线 SSE 连接。

        返回 True = 已投递给至少一个在线连接；False = 用户当前无在线连接
        （由调用方决定转投其它渠道或存入离线缓冲，见 hold_offline）。
        """
        if not event.user_id:
            return False

        async with self._lock:
            queues = list(self._queues.get(event.user_id, set()))

        if not queues:
            return False

        payload = event.model_dump()

        for queue in queues:
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                try:
                    _ = queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    queue.put_nowait(payload)
                except asyncio.QueueFull:
                    pass

        self._counters["delivered"] += 1
        return True

    async def hold_offline(self, event: TaskEvent) -> None:
        """把可见消息暂存进离线缓冲，等用户重新上线（建立 SSE）时回放补发。"""
        if not event.user_id:
            return
        payload = event.model_dump()
        async with self._lock:
            self._prune_offline(event.user_id)
            buf = self._offline.setdefault(event.user_id, [])
            if len(buf) >= MAX_OFFLINE_PER_USER:
                buf.pop(0)
                self._counters["offline_evicted"] += 1
            buf.append((time.time(), payload))
            self._counters["offline_held"] += 1

    async def note(self, name: str, delta: int = 1) -> None:
        """记录一个计数（如转投其它渠道成功 forwarded_other）。"""
        async with self._lock:
            if name in self._counters:
                self._counters[name] += delta

    async def stats(self) -> dict:
        """只读状态快照，供 /api/debug/sse_state 观测。"""
        async with self._lock:
            online = {uid: len(qs) for uid, qs in self._queues.items() if qs}
            for uid in list(self._offline.keys()):
                self._prune_offline(uid)
            offline = {uid: len(buf) for uid, buf in self._offline.items() if buf}
            return {
                "online_users": online,
                "online_total": sum(online.values()),
                "offline_buffered": offline,
                "offline_buffered_total": sum(offline.values()),
                "agent_buffer_keys": len(self._agent_buffer),
                "counters": dict(self._counters),
                "max_offline_per_user": MAX_OFFLINE_PER_USER,
                "offline_ttl_seconds": OFFLINE_TTL_SECONDS,
            }

    async def event_stream(
        self,
        user_id: str,
        agent_id: str | None = None,
    ) -> AsyncIterator[str]:
        queue = await self.subscribe(user_id, agent_id)

        try:
            yield ": connected\n\n"

            while True:
                try:
                    data = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                # 注意：这里**不按 agent_id 扣留事件**。
                # 原逻辑在「事件 agent != 订阅 agent」时把该事件塞进 _agent_buffer
                # 等切换对话再回放，结果是：用户在 A 对话里只看到 task_started，
                # 永远等不到回复（回复被扣在服务端）；gateway 重启还会连带丢失。
                # 现在一律投递，由前端按 metadata.agent_id 决定展示/归属。
                text = json.dumps(data, ensure_ascii=False)
                yield f"data: {text}\n\n"

        finally:
            await self.unsubscribe(user_id, queue)

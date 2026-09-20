import asyncio
import json
from collections import defaultdict
from typing import AsyncIterator, Dict, List, Set

from schemas import TaskEvent

MAX_AGENT_BUFFER = 100


class SSEHub:
    def __init__(self) -> None:
        self._queues: Dict[str, Set[asyncio.Queue]] = defaultdict(set)
        self._lock = asyncio.Lock()
        # 跨 agent buffer: (user_id, agent_id) -> [payload]
        self._agent_buffer: Dict[str, List[dict]] = defaultdict(list)
        # 轻量计数，供 /api/debug/sse_state 观测
        self._counters = {
            "delivered": 0,          # 成功投递给在线连接的事件数
            "nacked": 0,             # 用户离线、已回报投递失败（交调度器处置）的条数
        }

    def _buffer_key(self, user_id: str, agent_id: str) -> str:
        return f"{user_id}::{agent_id}"

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
        # 注意：用户离线期间积压的可见消息**不在本地缓冲**，由调度器持有，
        # 上线时通过 /api/events 里的 flush_buffered 通知它补发。
        for payload in agent_buf:
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
        （调用方据此回报投递失败，由**调度器**决定转投其它渠道或缓冲）。
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

    async def note(self, name: str, delta: int = 1) -> None:
        """记录一个计数（如转投其它渠道成功 forwarded_other）。"""
        async with self._lock:
            if name in self._counters:
                self._counters[name] += delta

    async def stats(self) -> dict:
        """只读状态快照，供 /api/debug/sse_state 观测。"""
        async with self._lock:
            online = {uid: len(qs) for uid, qs in self._queues.items() if qs}
            return {
                "online_users": online,
                "online_total": sum(online.values()),
                "agent_buffer_keys": len(self._agent_buffer),
                "counters": dict(self._counters),
                # 转投与缓冲的所有者是调度器（task-scheduler-service），
                # 本端点不再反映它们。
                "buffering_owner": "task-scheduler-service",
            }

    async def event_stream(
        self,
        user_id: str,
        agent_id: str | None = None,
        on_idle=None,
    ) -> AsyncIterator[str]:
        queue = await self.subscribe(user_id, agent_id)

        try:
            yield ": connected\n\n"

            while True:
                try:
                    data = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    # 空闲心跳。顺便再要一次补发：事件如果在用户**连上之后**才进缓冲
                    # （例如本次 flush 与入缓冲竞态），只在建立连接时补发一次会把它
                    # 一直留到下次连接 —— 而"用户在线却收不到"正是最该修的情况。
                    if on_idle is not None:
                        try:
                            await on_idle()
                        except Exception:
                            pass
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

import asyncio
import json
from collections import defaultdict
from typing import AsyncIterator, Dict, List, Set

from schemas import TaskEvent

MAX_OFFLINE_BUFFER = 200
MAX_AGENT_BUFFER = 100


class SSEHub:
    def __init__(self) -> None:
        self._queues: Dict[str, Set[asyncio.Queue]] = defaultdict(set)
        self._lock = asyncio.Lock()
        # 离线 buffer: user_id -> [payload]
        self._offline_buffer: Dict[str, List[dict]] = defaultdict(list)
        # 跨 agent buffer: (user_id, agent_id) -> [payload]
        self._agent_buffer: Dict[str, List[dict]] = defaultdict(list)

    def _buffer_key(self, user_id: str, agent_id: str) -> str:
        return f"{user_id}::{agent_id}"

    async def subscribe(self, user_id: str, agent_id: str | None = None) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=200)
        async with self._lock:
            self._queues[user_id].add(queue)
            # 回放离线 buffer
            offline = self._offline_buffer.pop(user_id, [])
            # 回放当前 agent 的跨 agent buffer
            if agent_id:
                key = self._buffer_key(user_id, agent_id)
                agent_buf = self._agent_buffer.pop(key, [])
            else:
                agent_buf = []
        # 先放离线事件，再放跨 agent 事件
        for payload in offline + agent_buf:
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

    async def publish(self, event: TaskEvent) -> None:
        if not event.user_id:
            return

        async with self._lock:
            queues = list(self._queues.get(event.user_id, set()))

        payload = event.model_dump()

        if not queues:
            # 用户离线：缓存到 offline buffer
            async with self._lock:
                buf = self._offline_buffer[event.user_id]
                if len(buf) >= MAX_OFFLINE_BUFFER:
                    buf.pop(0)
                buf.append(payload)
            return

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
                if agent_id:
                    meta = data.get("metadata", {})
                    event_agent = meta.get("agent_id") or data.get("agent_id", "")
                    if event_agent and event_agent != agent_id:
                        # 不丢弃，缓存到 agent_buffer 等切换时回放
                        key = self._buffer_key(user_id, event_agent)
                        async with self._lock:
                            buf = self._agent_buffer[key]
                            if len(buf) >= MAX_AGENT_BUFFER:
                                buf.pop(0)
                            buf.append(data)
                        continue
                text = json.dumps(data, ensure_ascii=False)
                yield f"data: {text}\n\n"

        finally:
            await self.unsubscribe(user_id, queue)

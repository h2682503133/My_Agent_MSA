import asyncio
import json
from collections import defaultdict
from typing import AsyncIterator, Dict, Set

from schemas import TaskEvent


class SSEHub:
    def __init__(self) -> None:
        self._queues: Dict[str, Set[asyncio.Queue]] = defaultdict(set)
        self._lock = asyncio.Lock()

    async def subscribe(self, user_id: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=200)
        async with self._lock:
            self._queues[user_id].add(queue)
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

        if not queues:
            return

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

    async def event_stream(
        self,
        user_id: str,
        agent_id: str | None = None,
    ) -> AsyncIterator[str]:
        queue = await self.subscribe(user_id)

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
                        continue
                text = json.dumps(data, ensure_ascii=False)
                yield f"data: {text}\n\n"

        finally:
            await self.unsubscribe(user_id, queue)

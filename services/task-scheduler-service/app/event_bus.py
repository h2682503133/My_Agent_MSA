import queue
import threading
import uuid
from typing import Iterable

from app.scheduled_task import ScheduledTask


class EventBus:
    def __init__(self):
        self._lock = threading.Lock()
        self._subscribers: dict[str, tuple[set[str], queue.Queue]] = {}

    def subscribe(self, subscriber_id: str, channels: Iterable[str]):
        channel_set = set(channels or [])
        q: queue.Queue = queue.Queue(maxsize=500)
        with self._lock:
            self._subscribers[subscriber_id] = (channel_set, q)
        try:
            while True:
                yield q.get()
        finally:
            with self._lock:
                self._subscribers.pop(subscriber_id, None)

    def publish(self, event: dict):
        event_channel = event.get("channel", "")
        dead = []
        with self._lock:
            items = list(self._subscribers.items())
        for subscriber_id, (channels, q) in items:
            if channels and event_channel not in channels:
                continue
            try:
                q.put_nowait(event)
            except queue.Full:
                dead.append(subscriber_id)
        if dead:
            with self._lock:
                for subscriber_id in dead:
                    self._subscribers.pop(subscriber_id, None)


event_bus = EventBus()


def make_event_id() -> str:
    return f"event-{uuid.uuid4().hex[:12]}"


def task_event(
    task: ScheduledTask,
    event_type: str,
    text: str = "",
    images: list[str] | None = None,
    waiting: int = 0,
    error: str = "",
    metadata: dict[str, str] | None = None,
) -> dict:
    return {
        "event_id": make_event_id(),
        "task_id": task.task_id,
        "user_id": task.user_id,
        "session_id": task.session_id,
        "channel": task.channel,
        "type": event_type,
        "text": text,
        "images": images or [],
        "waiting": waiting,
        "error": error,
        "delivery_target": task.delivery_target.to_dict(),
        "metadata": metadata or {},
    }

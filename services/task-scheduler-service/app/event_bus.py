import queue
import threading
import uuid
from typing import Iterable

from app.scheduled_task import ScheduledTask

MAX_BUFFER_PER_USER = 200

# 订阅流心跳间隔（秒）：subscribe() 每隔该时间 yield 一次 None（心跳），
# 让上层（server.SubscribeEvents）有机会检查 context.is_active()。
# 必须存在：否则客户端断开后 handler 会永久阻塞在 q.get() 上，
# 逐个泄漏 gRPC 工作线程，最终线程池耗尽导致 CreateTask 等 RPC 全部排队卡死。
# 只影响"客户端已断开"的检测延迟，不影响正常事件投递（事件到达即立即 yield）。
SUBSCRIBE_HEARTBEAT_SECONDS = 10.0


class EventBus:
    def __init__(self):
        self._lock = threading.Lock()
        self._subscribers: dict[str, tuple[set[str], queue.Queue]] = {}
        # 未送达事件 buffer: user_id -> [event]
        self._buffer: dict[str, list[dict]] = {}
        # user_client 由外部注入
        self._user_client = None

    def set_user_client(self, client):
        self._user_client = client

    def _get_user_channels(self, user_id: str) -> set[str]:
        if self._user_client is None:
            return set()
        try:
            return set(self._user_client.get_channels(user_id))
        except Exception:
            return set()

    def subscribe(self, subscriber_id: str, channels: Iterable[str]):
        channel_set = set(channels or [])
        q: queue.Queue = queue.Queue(maxsize=500)
        with self._lock:
            self._subscribers[subscriber_id] = (channel_set, q)
            # 回放 buffer 中属于这些渠道的用户的未送达事件
            to_replay = []
            for user_id, events in list(self._buffer.items()):
                user_channels = self._get_user_channels(user_id)
                # 如果用户的任一渠道在当前订阅的渠道集合中，回放
                if user_channels & channel_set:
                    to_replay.append(user_id)
            for user_id in to_replay:
                for event in self._buffer.pop(user_id, []):
                    try:
                        q.put_nowait(event)
                    except queue.Full:
                        break
        try:
            while True:
                try:
                    event = q.get(timeout=SUBSCRIBE_HEARTBEAT_SECONDS)
                except queue.Empty:
                    # 心跳：上层收到 None 会检查客户端是否还在，断开则退出并释放线程
                    yield None
                    continue
                yield event
        finally:
            with self._lock:
                # 只在「自己仍是当前注册者」时移除订阅。
                # 关键：客户端重连时用同一个 subscriber_id 覆盖注册新订阅，
                # 旧 handler 随后退出；若无条件 pop，会把**新订阅**删掉，
                # 导致之后所有事件都投不出去（只能等下次重连时靠 buffer 回放），
                # 表现为"消息只在重连瞬间才到、平时收不到"。
                entry = self._subscribers.get(subscriber_id)
                if entry is not None and entry[1] is q:
                    self._subscribers.pop(subscriber_id, None)

    def publish(self, event: dict):
        event_channel = event.get("channel", "")
        user_id = event.get("user_id", "")

        with self._lock:
            items = list(self._subscribers.items())

        # 第一优先：匹配原始 channel 的订阅者
        for subscriber_id, (channels, q) in items:
            if channels and event_channel not in channels:
                continue
            try:
                q.put_nowait(event)
                return  # 投递成功
            except queue.Full:
                pass

        # 第二优先：跨渠道 fallback，查用户绑定渠道
        if user_id:
            user_channels = self._get_user_channels(user_id)
            for alt_channel in user_channels:
                if alt_channel == event_channel:
                    continue
                for subscriber_id, (channels, q) in items:
                    if channels and alt_channel not in channels:
                        continue
                    fallback_event = dict(event)
                    fallback_event["channel"] = alt_channel
                    fallback_event["_fallback_from"] = event_channel
                    try:
                        q.put_nowait(fallback_event)
                        return  # 跨渠道投递成功
                    except queue.Full:
                        pass

        # 兜底：buffer 等待任意渠道上线
        with self._lock:
            if user_id not in self._buffer:
                self._buffer[user_id] = []
            buf = self._buffer[user_id]
            if len(buf) >= MAX_BUFFER_PER_USER:
                buf.pop(0)
            buf.append(event)


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

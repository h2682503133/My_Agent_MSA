"""
异步 gRPC 客户端，用于 qq-llbot-service 与 task-scheduler-service 通信。
"""
import os
import sys
from pathlib import Path

import grpc

PROTO_GEN_DIR = Path(__file__).parent.parent / "proto_gen"
if str(PROTO_GEN_DIR) not in sys.path:
    sys.path.insert(0, str(PROTO_GEN_DIR))

try:
    import task_scheduler_pb2
    import task_scheduler_pb2_grpc
except ImportError as e:
    raise RuntimeError(
        "gRPC generated files not found. Run bash scripts/gen_proto.sh first."
    ) from e


class SchedulerClient:
    def __init__(self, target: str):
        self.target = target

    async def create_task(
        self,
        user_id: str,
        content: str,
        channel: str = "qq",
        session_id: str = "",
        agent_id: str = "",
        client_message_id: str = "",
        images: list[str] | None = None,
    ) -> dict:
        session_id = session_id or f"{channel}_{user_id}"

        try:
            # 图片以 base64 data URL 传输，放宽 gRPC 单条消息大小上限（128 MiB）
            max_msg_bytes = 128 * 1024 * 1024
            async with grpc.aio.insecure_channel(
                self.target,
                options=[
                    ("grpc.max_send_message_length", max_msg_bytes),
                    ("grpc.max_receive_message_length", max_msg_bytes),
                ],
            ) as chan:
                stub = task_scheduler_pb2_grpc.TaskSchedulerStub(chan)
                req = task_scheduler_pb2.CreateTaskRequest(
                    user_id=user_id,
                    session_id=session_id,
                    channel=channel,
                    content=content,
                    client_message_id=client_message_id,
                    images=list(images or []),
                    agent_id=agent_id,
                    delivery_target=task_scheduler_pb2.DeliveryTarget(
                        channel=channel,
                        user_id=user_id,
                        conversation_id=session_id,
                        reply_to=client_message_id,
                    ),
                )
                resp = await stub.CreateTask(req, timeout=10)
                return {
                    "ok": resp.ok,
                    "task_id": resp.task_id,
                    "status": resp.status,
                    "waiting": resp.waiting,
                    "error": resp.error,
                }
        except Exception as e:
            return {"ok": False, "task_id": "", "status": "error", "waiting": 0, "error": str(e)}

    async def ack_event(
        self,
        subscriber_id: str,
        event_id: str,
        ok: bool,
        error: str = "",
    ) -> str:
        """回报投递结果。返回 disposition（acked / forwarded:<ch> / buffered / unknown）。"""
        try:
            async with grpc.aio.insecure_channel(self.target) as chan:
                stub = task_scheduler_pb2_grpc.TaskSchedulerStub(chan)
                resp = await stub.AckEvent(
                    task_scheduler_pb2.AckEventRequest(
                        subscriber_id=subscriber_id,
                        event_id=event_id,
                        ok=ok,
                        error=error,
                    ),
                    timeout=5,
                )
                return resp.disposition or ("acked" if resp.accepted else "unknown")
        except Exception as e:
            print(f"[qq-llbot] ack_event failed event={event_id}: {e}", flush=True)
            return "error"

    async def flush_buffered(self, user_id: str = "", channel: str = "qq") -> int:
        """通知调度器 QQ 渠道已可达，补发缓冲的事件。"""
        try:
            async with grpc.aio.insecure_channel(self.target) as chan:
                stub = task_scheduler_pb2_grpc.TaskSchedulerStub(chan)
                resp = await stub.FlushBuffered(
                    task_scheduler_pb2.FlushBufferedRequest(user_id=user_id, channel=channel),
                    timeout=5,
                )
                return int(resp.flushed or 0)
        except Exception as e:
            print(f"[qq-llbot] flush_buffered failed user={user_id}: {e}", flush=True)
            return 0

    async def subscribe_events(self, subscriber_id: str, channels: list[str]):
        """订阅事件流，带**空闲看门狗**。

        为什么必须有：这条流在没有任务事件时是静默的（scheduler 的心跳标记
        `None` 不会下发给客户端），所以 TCP 半开 / 对端 pod 被替换时，
        `async for` 会永远等不到数据也不抛错 —— 订阅者实际已不在对端的注册表里
        （实测：调度器被 rollout 后，它的日志持续报 `no subscriber for channel='qq'`，
        而 qq-llbot 这边毫无察觉），之后所有 QQ 事件都只能走转投或缓冲。

        超过 N 秒没有任何事件 → 主动断开重连，重新注册订阅。
        与 gateway-backend-service 的同名逻辑保持一致。
        """
        import asyncio

        idle_reconnect = float(os.getenv("SUBSCRIBE_IDLE_RECONNECT_SECONDS", "120"))

        while True:
            try:
                print(
                    f"[qq-llbot] subscribe scheduler events "
                    f"target={self.target} subscriber_id={subscriber_id}",
                    flush=True,
                )
                async with grpc.aio.insecure_channel(self.target) as chan:
                    stub = task_scheduler_pb2_grpc.TaskSchedulerStub(chan)
                    req = task_scheduler_pb2.SubscribeEventsRequest(
                        subscriber_id=subscriber_id,
                        channels=channels,
                    )
                    stream = stub.SubscribeEvents(req)
                    while True:
                        try:
                            event = await asyncio.wait_for(stream.read(), timeout=idle_reconnect)
                        except asyncio.TimeoutError:
                            print(
                                f"[qq-llbot] scheduler event subscription idle "
                                f"> {idle_reconnect:.0f}s no event, reconnecting",
                                flush=True,
                            )
                            break
                        if event is None:
                            break
                        yield {
                            "event_id": event.event_id,
                            "task_id": event.task_id,
                            "user_id": event.user_id,
                            "session_id": event.session_id,
                            "channel": event.channel,
                            "type": event.type,
                            "text": event.text,
                            "images": list(event.images),
                            "waiting": event.waiting,
                            "error": event.error,
                            "metadata": dict(event.metadata),
                        }
            except Exception as e:
                print(f"[qq-llbot] event subscription disconnected: {e}", flush=True)
                import asyncio as _asyncio
                await _asyncio.sleep(2)

import asyncio
import os
import uuid
from typing import AsyncIterator, List

from schemas import CreateTaskResult, DeliveryTarget, FrontendMessage, TaskEvent


class SchedulerClient:
    async def create_task(self, message: FrontendMessage) -> CreateTaskResult:
        raise NotImplementedError

    async def ack_event(
        self,
        subscriber_id: str,
        event_id: str,
        ok: bool,
        error: str = "",
    ) -> str:
        """回报投递结果（ok=false → 调度器转投其它渠道，全失败再缓冲）。"""
        raise NotImplementedError

    async def flush_buffered(self, user_id: str = "", channel: str = "web") -> int:
        """渠道可达时通知调度器补发缓冲事件。"""
        raise NotImplementedError

    async def subscribe_events(
        self,
        subscriber_id: str,
        channels: List[str],
    ) -> AsyncIterator[TaskEvent]:
        raise NotImplementedError


class MockSchedulerClient(SchedulerClient):
    def __init__(self) -> None:
        self._event_queue: asyncio.Queue = asyncio.Queue()

    async def ack_event(
        self,
        subscriber_id: str,
        event_id: str,
        ok: bool,
        error: str = "",
    ) -> str:
        # mock 模式无转投/缓冲语义
        return "acked" if ok else "unknown"

    async def flush_buffered(self, user_id: str = "", channel: str = "web") -> int:
        return 0

    async def create_task(self, message: FrontendMessage) -> CreateTaskResult:
        task_id = f"task-{uuid.uuid4().hex[:12]}"
        session_id = message.session_id or f"web_{message.user_id}"
        agent_id = message.agent_id or "main"  # kept for metadata

        await self._event_queue.put(
            TaskEvent(
                event_id=f"event-{uuid.uuid4().hex[:12]}",
                task_id=task_id,
                user_id=message.user_id,
                session_id=session_id,
                channel="web",
                type="task_queued",
                waiting=0,
                delivery_target=DeliveryTarget(
                    channel="web",
                    user_id=message.user_id,
                    conversation_id=session_id,
                    reply_to=message.client_message_id or "",
                ),
                metadata={"agent_id": agent_id},
            )
        )

        asyncio.create_task(self._mock_run_task(task_id, message, agent_id))

        return CreateTaskResult(ok=True, task_id=task_id, status="queued", waiting=0)

    async def _mock_run_task(
        self,
        task_id: str,
        message: FrontendMessage,
        agent_id: str,
    ) -> None:
        session_id = message.session_id or f"web_{message.user_id}"

        await asyncio.sleep(0.4)
        await self._event_queue.put(
            TaskEvent(
                event_id=f"event-{uuid.uuid4().hex[:12]}",
                task_id=task_id,
                user_id=message.user_id,
                session_id=session_id,
                channel="web",
                type="task_started",
                text="Mock task started.",
                metadata={"agent_id": agent_id},
            )
        )

        await asyncio.sleep(0.8)
        await self._event_queue.put(
            TaskEvent(
                event_id=f"event-{uuid.uuid4().hex[:12]}",
                task_id=task_id,
                user_id=message.user_id,
                session_id=session_id,
                channel="web",
                type="assistant_message",
                text=f"[{agent_id}] Mock gateway reply: {message.content}",
                images=[],
                metadata={"agent_id": agent_id, "visible_to_user": "true", "final": "true"},
            )
        )

        await asyncio.sleep(0.2)
        await self._event_queue.put(
            TaskEvent(
                event_id=f"event-{uuid.uuid4().hex[:12]}",
                task_id=task_id,
                user_id=message.user_id,
                session_id=session_id,
                channel="web",
                type="task_finished",
                waiting=0,
                metadata={"agent_id": agent_id},
            )
        )

    async def subscribe_events(
        self,
        subscriber_id: str,
        channels: List[str],
    ) -> AsyncIterator[TaskEvent]:
        while True:
            event = await self._event_queue.get()
            if not channels or event.channel in channels:
                yield event


class GrpcSchedulerClient(SchedulerClient):
    def __init__(self, target: str) -> None:
        self.target = target

    @staticmethod
    def _import_proto_modules():
        try:
            from proto_gen import scheduler_pb2, scheduler_pb2_grpc
        except ImportError:
            import scheduler_pb2
            import scheduler_pb2_grpc
        return scheduler_pb2, scheduler_pb2_grpc

    @staticmethod
    def _channel(message: FrontendMessage) -> str:
        """投递渠道：默认 web；离线转投其他渠道（如 qq）时由 message.channel 指定。"""
        return (message.channel or "web").strip() or "web"

    @staticmethod
    def _session_id(message: FrontendMessage) -> str:
        channel = GrpcSchedulerClient._channel(message)
        return message.session_id or f"{channel}_{message.user_id}"

    async def create_task(self, message: FrontendMessage) -> CreateTaskResult:
        import grpc

        scheduler_pb2, scheduler_pb2_grpc = self._import_proto_modules()
        channel_name = self._channel(message)
        session_id = self._session_id(message)
        agent_id = message.agent_id or "main"  # kept for metadata

        metadata = dict(message.metadata)
        metadata.setdefault("agent_id", agent_id)

        try:
            # 图片以 base64 data URL 传输，放宽 gRPC 单条消息大小上限（128 MiB）
            max_msg_bytes = 128 * 1024 * 1024
            async with grpc.aio.insecure_channel(
                self.target,
                options=[
                    ("grpc.max_send_message_length", max_msg_bytes),
                    ("grpc.max_receive_message_length", max_msg_bytes),
                ],
            ) as channel:
                stub = scheduler_pb2_grpc.TaskSchedulerStub(channel)

                req = scheduler_pb2.CreateTaskRequest(
                    user_id=message.user_id,
                    session_id=session_id,
                    channel=channel_name,
                    content=message.content,
                    client_message_id=message.client_message_id or "",
                    images=list(message.images or []),
                    delivery_target=scheduler_pb2.DeliveryTarget(
                        channel=channel_name,
                        user_id=message.user_id,
                        conversation_id=session_id,
                        reply_to=message.client_message_id or "",
                    ),
                    metadata=metadata,
                    agent_id=agent_id,
                )

                resp = await stub.CreateTask(req)

        except Exception as exc:
            return CreateTaskResult(
                ok=False,
                status="error",
                error=f"failed to call task-scheduler-service at {self.target}: {exc}",
            )

        return CreateTaskResult(
            ok=resp.ok,
            task_id=resp.task_id,
            status=resp.status,
            waiting=resp.waiting,
            error=resp.error,
        )

    async def ack_event(
        self,
        subscriber_id: str,
        event_id: str,
        ok: bool,
        error: str = "",
    ) -> str:
        """回报投递结果。返回 disposition（acked / forwarded:<ch> / buffered / unknown）。"""
        import grpc

        scheduler_pb2, scheduler_pb2_grpc = self._import_proto_modules()
        try:
            async with grpc.aio.insecure_channel(self.target) as channel:
                stub = scheduler_pb2_grpc.TaskSchedulerStub(channel)
                resp = await stub.AckEvent(
                    scheduler_pb2.AckEventRequest(
                        subscriber_id=subscriber_id,
                        event_id=event_id,
                        ok=ok,
                        error=error,
                    ),
                    timeout=5,
                )
                return resp.disposition or ("acked" if resp.accepted else "unknown")
        except Exception as exc:
            print(f"[gateway] ack_event failed event={event_id}: {exc}", flush=True)
            return "error"

    async def flush_buffered(self, user_id: str = "", channel: str = "web") -> int:
        """通知调度器本渠道（或该用户在本渠道）已可达，补发缓冲的事件。"""
        import grpc

        scheduler_pb2, scheduler_pb2_grpc = self._import_proto_modules()
        try:
            async with grpc.aio.insecure_channel(self.target) as channel_conn:
                stub = scheduler_pb2_grpc.TaskSchedulerStub(channel_conn)
                resp = await stub.FlushBuffered(
                    scheduler_pb2.FlushBufferedRequest(user_id=user_id, channel=channel),
                    timeout=5,
                )
                return int(resp.flushed or 0)
        except Exception as exc:
            print(f"[gateway] flush_buffered failed user={user_id}: {exc}", flush=True)
            return 0

    async def subscribe_events(
        self,
        subscriber_id: str,
        channels: List[str],
    ) -> AsyncIterator[TaskEvent]:
        import grpc

        scheduler_pb2, scheduler_pb2_grpc = self._import_proto_modules()

        # 空闲看门狗：这条订阅流在无任务事件时是静默的（scheduler 不发送心跳）。
        # TCP 半开/对端静默清理连接时，async for 会永远等不到数据也不抛错 → 永不重连，
        # 之后所有事件都堆积在 scheduler 的 event_bus buffer 里，web 端收不到任何回复。
        # 因此：超过 N 秒没有任何事件 → 主动断开本次订阅并重连。
        # 重连时 scheduler 会回放 buffer 中属于用户渠道的事件，错过的回复自动补发。
        idle_reconnect = float(os.getenv("SUBSCRIBE_IDLE_RECONNECT_SECONDS", "120"))

        while True:
            try:
                print(
                    f"[gateway] subscribe scheduler events target={self.target} subscriber_id={subscriber_id}",
                    flush=True,
                )

                async with grpc.aio.insecure_channel(self.target) as channel:
                    stub = scheduler_pb2_grpc.TaskSchedulerStub(channel)

                    req = scheduler_pb2.SubscribeEventsRequest(
                        subscriber_id=subscriber_id,
                        channels=channels,
                    )

                    stream = stub.SubscribeEvents(req)
                    while True:
                        try:
                            # 每条事件等待上限：超时视为订阅失活，断开重连
                            event = await asyncio.wait_for(
                                stream.read(),
                                timeout=idle_reconnect,
                            )
                        except asyncio.TimeoutError:
                            print(
                                "[gateway] scheduler event subscription idle "
                                f"> {idle_reconnect:.0f}s no event, reconnecting",
                                flush=True,
                            )
                            break
                        if event is None:
                            # 流正常结束
                            break

                        delivery_target = None

                        if event.delivery_target and event.delivery_target.user_id:
                            delivery_target = DeliveryTarget(
                                channel=event.delivery_target.channel,
                                user_id=event.delivery_target.user_id,
                                conversation_id=event.delivery_target.conversation_id,
                                reply_to=event.delivery_target.reply_to,
                            )

                        yield TaskEvent(
                            event_id=event.event_id,
                            task_id=event.task_id,
                            user_id=event.user_id,
                            session_id=event.session_id,
                            channel=event.channel,
                            type=event.type,
                            text=event.text,
                            images=list(event.images),
                            waiting=event.waiting,
                            error=event.error,
                            delivery_target=delivery_target,
                            metadata=dict(event.metadata),
                        )

            except asyncio.CancelledError:
                print("[gateway] scheduler event subscription cancelled", flush=True)
                raise
            except Exception as exc:
                print(f"[gateway] scheduler event subscription disconnected: {exc}", flush=True)
                await asyncio.sleep(2)


def build_scheduler_client() -> SchedulerClient:
    mode = os.getenv("SCHEDULER_CLIENT_MODE", "grpc").lower()

    if mode == "mock":
        return MockSchedulerClient()

    target = os.getenv(
        "SCHEDULER_GRPC_TARGET",
        "task-scheduler-service.agent.svc.cluster.local:5100",
    )
    return GrpcSchedulerClient(target=target)

import asyncio
import os
import uuid
from typing import AsyncIterator, List

from schemas import CreateTaskResult, DeliveryTarget, FrontendMessage, TaskEvent


class SchedulerClient:
    async def create_task(self, message: FrontendMessage) -> CreateTaskResult:
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

    async def create_task(self, message: FrontendMessage) -> CreateTaskResult:
        task_id = f"task-{uuid.uuid4().hex[:12]}"
        session_id = message.session_id or f"web_{message.user_id}"
        agent_id = message.agent_id or "main"

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
    def _session_id(message: FrontendMessage) -> str:
        agent_id = message.agent_id or "main"
        return message.session_id or f"web_{message.user_id}_{agent_id}"

    async def create_task(self, message: FrontendMessage) -> CreateTaskResult:
        import grpc

        scheduler_pb2, scheduler_pb2_grpc = self._import_proto_modules()
        session_id = self._session_id(message)
        agent_id = message.agent_id or "main"

        metadata = dict(message.metadata)
        metadata.setdefault("agent_id", agent_id)

        try:
            async with grpc.aio.insecure_channel(self.target) as channel:
                stub = scheduler_pb2_grpc.TaskSchedulerStub(channel)

                req = scheduler_pb2.CreateTaskRequest(
                    user_id=message.user_id,
                    session_id=session_id,
                    channel="web",
                    content=message.content,
                    client_message_id=message.client_message_id or "",
                    delivery_target=scheduler_pb2.DeliveryTarget(
                        channel="web",
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

    async def subscribe_events(
        self,
        subscriber_id: str,
        channels: List[str],
    ) -> AsyncIterator[TaskEvent]:
        import grpc

        scheduler_pb2, scheduler_pb2_grpc = self._import_proto_modules()

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

                    async for event in stub.SubscribeEvents(req):
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

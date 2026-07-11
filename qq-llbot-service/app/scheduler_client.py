"""
异步 gRPC 客户端，用于 qq-llbot-service 与 task-scheduler-service 通信。
"""
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
    ) -> dict:
        session_id = session_id or f"{channel}_{user_id}"

        try:
            async with grpc.aio.insecure_channel(self.target) as chan:
                stub = task_scheduler_pb2_grpc.TaskSchedulerStub(chan)
                req = task_scheduler_pb2.CreateTaskRequest(
                    user_id=user_id,
                    session_id=session_id,
                    channel=channel,
                    content=content,
                    client_message_id=client_message_id,
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

    async def subscribe_events(self, subscriber_id: str, channels: list[str]):
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
                    async for event in stub.SubscribeEvents(req):
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
                import asyncio
                await asyncio.sleep(2)

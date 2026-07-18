import sys
import uuid
from concurrent import futures
from pathlib import Path

import grpc

from app import config
from app.event_bus import event_bus
from app.orchestrator_client import OrchestratorClient
from app.scheduled_task import DeliveryTarget, ScheduledTask
from app.scheduler import start_scheduler, submit_task

GENERATED_DIR = Path(__file__).parent / "generated"
if str(GENERATED_DIR) not in sys.path:
    sys.path.insert(0, str(GENERATED_DIR))

try:
    import task_scheduler_pb2
    import task_scheduler_pb2_grpc
except ImportError as e:  # pragma: no cover
    raise RuntimeError("gRPC generated files not found. Run bash scripts/gen_proto.sh first.") from e


class TaskSchedulerService(task_scheduler_pb2_grpc.TaskSchedulerServicer):
    def CreateTask(self, request, context):
        """gateway/channel-gateway -> scheduler: create a lightweight ScheduledTask.

        Important boundary: this method does NOT build the old heavy Task and does
        not handle pending / push_context / pop_context. Those belong to
        agent-orchestrator-service's TaskRuntime.
        """
        try:
            user_id = request.user_id.strip()
            channel = request.channel or "web"
            session_id = request.session_id or f"{channel}_{user_id}"
            client_message_id = request.client_message_id
            agent_id = request.agent_id or ""
            content = request.content

            if not user_id:
                raise ValueError("user_id is required")
            if not content:
                raise ValueError("content is required")

            task_id = f"task-{uuid.uuid4().hex[:12]}"
            delivery_target = DeliveryTarget(
                channel=request.delivery_target.channel or channel,
                user_id=request.delivery_target.user_id or user_id,
                conversation_id=request.delivery_target.conversation_id or session_id,
                reply_to=request.delivery_target.reply_to or client_message_id,
            )
            metadata = dict(request.metadata)
            if client_message_id:
                metadata.setdefault("client_message_id", client_message_id)

            task = ScheduledTask(
                task_id=task_id,
                user_id=user_id,
                session_id=session_id,
                channel=channel,
                content=content,
                client_message_id=client_message_id,
                delivery_target=delivery_target,
                metadata=metadata,
                agent_id=agent_id,
            )

            result = submit_task(task)
            return task_scheduler_pb2.CreateTaskResponse(
                ok=result.ok,
                task_id=result.task_id,
                status=result.status,
                waiting=result.waiting,
                error=result.error,
            )
        except Exception as e:
            return task_scheduler_pb2.CreateTaskResponse(
                ok=False,
                task_id="",
                status="error",
                waiting=0,
                error=str(e),
            )

    def SubscribeEvents(self, request, context):
        subscriber_id = request.subscriber_id or f"subscriber-{uuid.uuid4().hex[:12]}"
        for event in event_bus.subscribe(subscriber_id, request.channels):
            if not context.is_active():
                break
            yield self._dict_to_task_event(event)

    def _dict_to_task_event(self, event: dict):
        delivery = event.get("delivery_target") or {}
        return task_scheduler_pb2.TaskEvent(
            event_id=event.get("event_id", ""),
            task_id=event.get("task_id", ""),
            user_id=event.get("user_id", ""),
            session_id=event.get("session_id", ""),
            channel=event.get("channel", ""),
            type=event.get("type", ""),
            text=event.get("text", ""),
            images=event.get("images", []),
            waiting=int(event.get("waiting", 0)),
            error=event.get("error", ""),
            delivery_target=task_scheduler_pb2.DeliveryTarget(
                channel=delivery.get("channel", ""),
                user_id=delivery.get("user_id", ""),
                conversation_id=delivery.get("conversation_id", ""),
                reply_to=delivery.get("reply_to", ""),
            ),
            metadata=event.get("metadata", {}),
        )


def serve():
    start_scheduler(OrchestratorClient(config.ORCHESTRATOR_TARGET))

    server = grpc.server(futures.ThreadPoolExecutor(max_workers=32))
    task_scheduler_pb2_grpc.add_TaskSchedulerServicer_to_server(TaskSchedulerService(), server)
    listen_addr = f"{config.SCHEDULER_HOST}:{config.SCHEDULER_PORT}"
    server.add_insecure_port(listen_addr)
    server.start()
    print(f"task-scheduler-service started on {listen_addr}", flush=True)
    print(f"agent-orchestrator target: {config.ORCHESTRATOR_TARGET}", flush=True)
    server.wait_for_termination()

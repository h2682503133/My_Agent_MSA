import sys
from pathlib import Path
from typing import Iterator

import grpc

from app import config
from app.event_bus import make_event_id
from app.scheduled_task import ScheduledTask

GENERATED_DIR = Path(__file__).parent / "generated"
if str(GENERATED_DIR) not in sys.path:
    sys.path.insert(0, str(GENERATED_DIR))

try:
    import agent_orchestrator_pb2
    import agent_orchestrator_pb2_grpc
except ImportError:  # pragma: no cover - only happens before running scripts/gen_proto.sh
    agent_orchestrator_pb2 = None
    agent_orchestrator_pb2_grpc = None


class OrchestratorClient:
    def __init__(self, target: str | None = None):
        self.target = target or config.ORCHESTRATOR_TARGET

    def execute_task(self, task: ScheduledTask) -> Iterator[dict]:
        """
        scheduler -> orchestrator: gRPC streaming ExecuteTask.

        The scheduler sends only a lightweight DTO. The orchestrator is responsible
        for constructing TaskRuntime / the old heavy Task and managing
        push_context/pop_context, pending, tool logs, model parsing, etc.
        """
        if agent_orchestrator_pb2 is None or agent_orchestrator_pb2_grpc is None:
            yield self._error_event(task, "gRPC generated files not found. Run bash scripts/gen_proto.sh first.")
            return

        request = agent_orchestrator_pb2.ExecuteTaskRequest(
            task_id=task.task_id,
            user_id=task.user_id,
            session_id=task.session_id,
            channel=task.channel,
            content=task.content,
            created_at=task.created_at_iso,
            agent_id=task.agent_id,
            metadata=task.to_execute_metadata(),
        )

        metadata = (
            ("x-task-id", task.task_id),
            ("x-user-id", task.user_id),
            ("x-session-id", task.session_id),
            ("x-channel", task.channel),
        )

        try:
            with grpc.insecure_channel(self.target) as channel:
                stub = agent_orchestrator_pb2_grpc.AgentOrchestratorStub(channel)
                stream = stub.ExecuteTask(
                    request,
                    timeout=None,  # 不再设总超时，由 MAX_AGENT_STEPS + 每轮 MODEL_TIMEOUT_SECONDS 自然限长
                    metadata=metadata,
                    wait_for_ready=True,
                )
                for event in stream:
                    yield self._event_to_dict(task, event)
        except Exception as e:
            yield self._error_event(task, f"agent-orchestrator-service 调用失败: {e}")

    def _event_to_dict(self, task: ScheduledTask, event) -> dict:
        return {
            "event_id": event.event_id or make_event_id(),
            "task_id": event.task_id or task.task_id,
            "user_id": event.user_id or task.user_id,
            "session_id": event.session_id or task.session_id,
            "channel": event.channel or task.channel,
            "type": event.type,
            "text": event.text,
            "images": list(event.images),
            "waiting": event.waiting,
            "error": event.error,
            "delivery_target": {
                "channel": event.delivery_target.channel or task.delivery_target.channel,
                "user_id": event.delivery_target.user_id or task.delivery_target.user_id,
                "conversation_id": event.delivery_target.conversation_id or task.delivery_target.conversation_id,
                "reply_to": event.delivery_target.reply_to or task.delivery_target.reply_to,
            },
            "metadata": dict(event.metadata),
        }

    def _error_event(self, task: ScheduledTask, error: str) -> dict:
        return {
            "event_id": make_event_id(),
            "task_id": task.task_id,
            "user_id": task.user_id,
            "session_id": task.session_id,
            "channel": task.channel,
            "type": "task_failed",
            "text": "",
            "images": [],
            "waiting": 0,
            "error": error,
            "delivery_target": task.delivery_target.to_dict(),
            "metadata": {},
        }

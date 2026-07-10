"""
gRPC client for calling back to task-scheduler-service when a timer task fires.
"""
import sys
from pathlib import Path

import grpc

from app import config
from app.logger import timer_log

GENERATED_DIR = Path(__file__).parent / "generated"
if str(GENERATED_DIR) not in sys.path:
    sys.path.insert(0, str(GENERATED_DIR))

try:
    import task_scheduler_pb2
    import task_scheduler_pb2_grpc
except ImportError as e:
    raise RuntimeError(
        "gRPC generated files not found. Run bash scripts/gen_proto.sh first."
    ) from e


class SchedulerClient:
    def __init__(self, target: str | None = None):
        self.target = target or config.SCHEDULER_TARGET
        self._channel = None
        self._stub = None

    @property
    def channel(self):
        if self._channel is None:
            self._channel = grpc.insecure_channel(self.target)
        return self._channel

    @property
    def stub(self):
        if self._stub is None:
            self._stub = task_scheduler_pb2_grpc.TaskSchedulerStub(self.channel)
        return self._stub

    def create_task(
        self,
        user_id: str,
        session_id: str,
        channel: str,
        content: str,
        client_message_id: str = "",
        agent_id: str = "",
        metadata: dict | None = None,
    ) -> dict:
        try:
            request = task_scheduler_pb2.CreateTaskRequest(
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
                metadata=metadata or {"source": "timer_task"},
            )
            response = self.stub.CreateTask(
                request, timeout=config.SCHEDULER_GRPC_DEADLINE
            )
            return {
                "ok": response.ok,
                "task_id": response.task_id,
                "status": response.status,
                "waiting": response.waiting,
                "error": response.error,
            }
        except Exception as e:
            timer_log(f"Failed to call scheduler CreateTask: {e}")
            return {"ok": False, "task_id": "", "status": "error", "waiting": 0, "error": str(e)}

    def close(self):
        if self._channel is not None:
            self._channel.close()
            self._channel = None
            self._stub = None

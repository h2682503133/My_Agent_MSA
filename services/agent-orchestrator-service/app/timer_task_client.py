"""
gRPC client for timer-task-service.
"""
import sys
from pathlib import Path

import grpc

from app import config
from app.logger import debug_log

GENERATED_DIR = Path(__file__).parent / "generated"
if str(GENERATED_DIR) not in sys.path:
    sys.path.insert(0, str(GENERATED_DIR))

try:
    import timer_task_pb2
    import timer_task_pb2_grpc
except ImportError as e:
    raise RuntimeError(
        "gRPC generated files not found. Run bash scripts/gen_proto.sh first."
    ) from e


class TimerTaskClient:
    def __init__(self, target: str | None = None):
        self.target = target or config.TIMER_TASK_TARGET

    def create_timer_task(
        self,
        user_id: str,
        session_id: str,
        channel_id: str,
        trigger_timestamp: float,
        content: str,
        task_type: str = "submit_task",
        agent_id: str = "",
        client_message_id: str = "",
    ) -> dict:
        try:
            with grpc.insecure_channel(self.target) as channel:
                stub = timer_task_pb2_grpc.TimerTaskStub(channel)
                request = timer_task_pb2.CreateTimerTaskRequest(
                    user_id=user_id,
                    session_id=session_id,
                    channel_id=channel_id,
                    trigger_timestamp=trigger_timestamp,
                    content=content,
                    task_type=task_type,
                    agent_id=agent_id,
                    client_message_id=client_message_id,
                )
                response = stub.CreateTimerTask(request, timeout=10)
                return {
                    "ok": response.ok,
                    "task_id": response.task_id,
                    "message": response.message,
                }
        except Exception as e:
            debug_log(f"TimerTaskClient.create_timer_task failed: {e}")
            return {"ok": False, "task_id": "", "message": str(e)}

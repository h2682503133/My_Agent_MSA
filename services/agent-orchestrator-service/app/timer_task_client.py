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
        time_str: str = "",
        repeat_str: str = "",
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
                    time_str=time_str,
                    repeat_str=repeat_str,
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

    def delete_user_task(self, user_id: str, task_id: str) -> dict:
        try:
            with grpc.insecure_channel(self.target) as channel:
                stub = timer_task_pb2_grpc.TimerTaskStub(channel)
                request = timer_task_pb2.DeleteUserTaskRequest(
                    user_id=user_id,
                    task_id=task_id,
                )
                response = stub.DeleteUserTask(request, timeout=10)
                return {
                    "ok": response.ok,
                    "message": response.message,
                }
        except Exception as e:
            debug_log(f"TimerTaskClient.delete_user_task failed: {e}")
            return {"ok": False, "message": str(e)}

    def list_user_tasks(self, user_id: str) -> dict:
        try:
            with grpc.insecure_channel(self.target) as channel:
                stub = timer_task_pb2_grpc.TimerTaskStub(channel)
                request = timer_task_pb2.ListUserTasksRequest(
                    user_id=user_id,
                )
                response = stub.ListUserTasks(request, timeout=10)
                tasks = [
                    {
                        "task_id": t.task_id,
                        "user_id": t.user_id,
                        "channel_id": t.channel_id,
                        "session_id": t.session_id,
                        "trigger_time": t.trigger_time,
                        "trigger_time_str": t.trigger_time_str,
                        "content": t.content,
                        "task_type": t.task_type,
                        "client_message_id": t.client_message_id,
                        "agent_id": t.agent_id,
                        "created_at": t.created_at,
                        "schedule_str": t.schedule_str,
                        "schedule": t.schedule,
                    }
                    for t in response.tasks
                ]
                return {"ok": True, "tasks": tasks, "message": ""}
        except Exception as e:
            debug_log(f"TimerTaskClient.list_user_tasks failed: {e}")
            return {"ok": False, "tasks": [], "message": str(e)}

import sys
from concurrent import futures
from pathlib import Path

import grpc

from app import config
from app.timer_task import (
    add_timer_task,
    delete_user_task,
    list_user_tasks,
    start_timer_service,
)

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


class TimerTaskService(timer_task_pb2_grpc.TimerTaskServicer):
    def CreateTimerTask(self, request, context):
        try:
            user_id = request.user_id.strip()
            if not user_id:
                return timer_task_pb2.CreateTimerTaskResponse(
                    ok=False, task_id="", message="user_id is required"
                )

            message = add_timer_task(
                user_id=user_id,
                channel_id=request.channel_id or "web",
                trigger_timestamp=request.trigger_timestamp,
                content=request.content or "system:auto_commit",
                task_type=request.task_type or "submit_task",
                session_id=request.session_id or None,
                client_message_id=request.client_message_id or "",
                agent_id=request.agent_id or "",
            )

            ok = not message.startswith("定时任务创建失败")
            return timer_task_pb2.CreateTimerTaskResponse(
                ok=ok, task_id="", message=message
            )
        except Exception as e:
            return timer_task_pb2.CreateTimerTaskResponse(
                ok=False, task_id="", message=str(e)
            )

    def ListUserTasks(self, request, context):
        try:
            tasks = list_user_tasks(request.user_id.strip())
            pb_tasks = [
                timer_task_pb2.TimerTaskInfo(
                    task_id=t.get("task_id", ""),
                    user_id=t.get("user_id", ""),
                    channel_id=t.get("channel_id", ""),
                    session_id=t.get("session_id", ""),
                    trigger_time=t.get("trigger_time", 0),
                    trigger_time_str=t.get("trigger_time_str", ""),
                    content=t.get("content", ""),
                    task_type=t.get("task_type", ""),
                    client_message_id=t.get("client_message_id", ""),
                    agent_id=t.get("agent_id", ""),
                    created_at=t.get("created_at", ""),
                )
                for t in tasks
            ]
            return timer_task_pb2.ListUserTasksResponse(tasks=pb_tasks)
        except Exception as e:
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(e))
            return timer_task_pb2.ListUserTasksResponse()

    def DeleteUserTask(self, request, context):
        try:
            message = delete_user_task(
                request.user_id.strip(), request.task_id.strip()
            )
            ok = not message.startswith("未找到") and not message.startswith("删除定时任务失败")
            return timer_task_pb2.DeleteUserTaskResponse(ok=ok, message=message)
        except Exception as e:
            return timer_task_pb2.DeleteUserTaskResponse(
                ok=False, message=str(e)
            )


def serve():
    start_timer_service()

    server = grpc.server(futures.ThreadPoolExecutor(max_workers=16))
    timer_task_pb2_grpc.add_TimerTaskServicer_to_server(TimerTaskService(), server)
    listen_addr = f"{config.TIMER_GRPC_HOST}:{config.TIMER_GRPC_PORT}"
    server.add_insecure_port(listen_addr)
    server.start()
    print(f"timer-task-service started on {listen_addr}", flush=True)
    server.wait_for_termination()

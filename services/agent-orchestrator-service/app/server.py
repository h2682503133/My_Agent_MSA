import queue
import sys
import threading
from concurrent import futures
from pathlib import Path

import grpc

from app import config
from app.agent_runtime import AgentRuntime
from app.logger import log
from app.task_runtime import TaskRuntime

GENERATED_DIR = Path(__file__).parent / "generated"
if str(GENERATED_DIR) not in sys.path:
    sys.path.insert(0, str(GENERATED_DIR))

try:
    import agent_orchestrator_pb2
    import agent_orchestrator_pb2_grpc
except ImportError as exc:
    raise RuntimeError("gRPC generated files not found. Run bash scripts/gen_proto.sh first.") from exc


def dto_to_pb(event):
    delivery = agent_orchestrator_pb2.DeliveryTarget()
    if event.delivery_target:
        delivery = agent_orchestrator_pb2.DeliveryTarget(
            channel=event.delivery_target.channel,
            user_id=event.delivery_target.user_id,
            conversation_id=event.delivery_target.conversation_id,
            reply_to=event.delivery_target.reply_to,
        )

    return agent_orchestrator_pb2.TaskEvent(
        event_id=event.event_id,
        task_id=event.task_id,
        user_id=event.user_id,
        session_id=event.session_id,
        channel=event.channel,
        type=event.type,
        text=event.text,
        images=event.images,
        waiting=event.waiting,
        error=event.error,
        delivery_target=delivery,
        metadata=event.metadata,
    )


class AgentOrchestratorService(agent_orchestrator_pb2_grpc.AgentOrchestratorServicer):
    def ExecuteTask(self, request, context):
        log(
            "ExecuteTask received "
            f"task_id={request.task_id} user_id={request.user_id} "
            f"session_id={request.session_id} channel={request.channel} agent_id={request.agent_id}"
        )

        task = TaskRuntime.from_execute_request(request)
        events_q: queue.Queue = queue.Queue()

        def emit(event):
            events_q.put(event)

        def run():
            try:
                AgentRuntime.process_task(task, emit)
            except Exception as exc:
                from app.events import TaskEventDTO, DeliveryTarget, new_event_id

                log(f"[{task.user.id}] ExecuteTask failed: {exc}")
                emit(TaskEventDTO(
                    event_id=new_event_id(),
                    task_id=task.task_id,
                    user_id=task.user.id,
                    session_id=task.user.session_id,
                    channel=task.channel,
                    type="task_failed",
                    text=str(exc),
                    error=str(exc),
                    delivery_target=DeliveryTarget(
                        channel=task.channel,
                        user_id=task.user.id,
                        conversation_id=task.user.session_id,
                        reply_to=task.metadata.get("client_message_id", ""),
                    ),
                    metadata={"final": "true"},
                ))
            finally:
                events_q.put(None)

        # 后台线程执行任务，主线程边收边 yield：工具执行过程中的即时事件
        # （图片/消息）可以立刻流式转发给下游，而不是攒到任务结束才发送。
        threading.Thread(target=run, daemon=True).start()

        while True:
            try:
                event = events_q.get(timeout=1)
            except queue.Empty:
                if not context.is_active():
                    break
                continue
            if event is None:
                break
            yield dto_to_pb(event)


def serve():
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=16))
    agent_orchestrator_pb2_grpc.add_AgentOrchestratorServicer_to_server(
        AgentOrchestratorService(),
        server,
    )

    listen_addr = f"[::]:{config.ORCHESTRATOR_GRPC_PORT}"
    server.add_insecure_port(listen_addr)
    server.start()
    log(f"agent-orchestrator-service started on {listen_addr}")
    server.wait_for_termination()

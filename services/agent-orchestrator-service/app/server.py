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
    def _stream_task_events(self, task, run_fn, context):
        """后台线程执行任务，主线程边收边 yield：工具执行过程中的即时事件
        （图片/消息）可以立刻流式转发给下游，而不是攒到任务结束才发送。"""
        events_q: queue.Queue = queue.Queue()

        def emit(event):
            events_q.put(event)

        def run():
            try:
                run_fn(task, emit)
            except Exception as exc:
                from app.events import TaskEventDTO, DeliveryTarget, new_event_id

                log(f"[{task.user.id}] task run failed: {exc}")
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

    def ExecuteTask(self, request, context):
        log(
            "ExecuteTask received "
            f"task_id={request.task_id} user_id={request.user_id} "
            f"session_id={request.session_id} channel={request.channel} agent_id={request.agent_id}"
        )

        task = TaskRuntime.from_execute_request(request)
        yield from self._stream_task_events(task, AgentRuntime.process_task, context)

    def ResumeTask(self, request, context):
        log(
            "ResumeTask received "
            f"task_id={request.task_id} user_id={request.user_id} "
            f"session_id={request.session_id} channel={request.channel} agent_id={request.agent_id}"
        )

        task = AgentRuntime.pop_pending_task(request.task_id)
        if task is None:
            # 挂起状态丢失（如 orchestrator 重启）：回退为普通新任务处理
            log(f"[{request.user_id}] pending task {request.task_id} not found, fallback to ExecuteTask")
            exec_request = agent_orchestrator_pb2.ExecuteTaskRequest(
                task_id=request.task_id,
                user_id=request.user_id,
                session_id=request.session_id,
                channel=request.channel,
                content=request.content,
                created_at=request.created_at,
                metadata=dict(request.metadata),
                agent_id=request.agent_id,
                images=list(request.images or []),
            )
            yield from self.ExecuteTask(exec_request, context)
            return

        # 恢复时用回复消息的 metadata（client_message_id）更新，保证最终回复的 reply_to 正确
        if request.metadata:
            task.metadata.update({k: v for k, v in request.metadata.items()})

        def run(task, emit):
            AgentRuntime.resume_task(
                task,
                request.content,
                emit,
                images=list(request.images or []),
            )

        yield from self._stream_task_events(task, run, context)

    def SetSessionState(self, request, context):
        """斜杠命令 /agent、/mode：改会话的默认智能体 / 模式（内存态）。"""
        ok, message, error = AgentRuntime.set_session_state(
            user_id=request.user_id,
            session_id=request.session_id,
            agent_id=request.agent_id,
            mode=request.mode,
        )
        return agent_orchestrator_pb2.SetSessionStateResponse(
            ok=ok, message=message, error=error
        )

    def StopTask(self, request, context):
        """打断（barge-in）：让该用户正在执行的任务在下一个检查点暂停。

        语义是**暂停**而不是取消：现场（上下文栈 / tool_log / 记忆）保留在
        orchestrator 内存里，用户下一条消息经 ResumeTask 回到同一处接着跑。

        这里不阻塞等待暂停落地 —— 暂停发生在任务自己的检查点，等待只会白占
        一个 gRPC 工作线程、并且也改变不了「当前这一步跑完才生效」的节奏。
        stopped=False 表示当时没有任务在跑（已结束或本来就空闲）。
        """
        log(
            "StopTask received "
            f"task_id={request.task_id} user_id={request.user_id} reason={request.reason}"
        )
        try:
            stopped = AgentRuntime.request_stop(
                user_id=request.user_id,
                task_id=request.task_id,
            )
            return agent_orchestrator_pb2.StopTaskResponse(
                ok=True,
                stopped=stopped,
                message="已请求暂停" if stopped else "没有正在执行的任务",
            )
        except Exception as exc:
            log(f"StopTask failed user_id={request.user_id}: {exc}")
            return agent_orchestrator_pb2.StopTaskResponse(
                ok=False,
                stopped=False,
                message=str(exc),
            )


def serve():
    # 图片以 base64 data URL 传输，放宽 gRPC 单条消息大小上限（128 MiB）
    max_msg_bytes = 128 * 1024 * 1024
    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=16),
        options=[
            ("grpc.max_send_message_length", max_msg_bytes),
            ("grpc.max_receive_message_length", max_msg_bytes),
        ],
    )
    agent_orchestrator_pb2_grpc.add_AgentOrchestratorServicer_to_server(
        AgentOrchestratorService(),
        server,
    )

    listen_addr = f"[::]:{config.ORCHESTRATOR_GRPC_PORT}"
    server.add_insecure_port(listen_addr)
    server.start()
    log(f"agent-orchestrator-service started on {listen_addr}")
    server.wait_for_termination()

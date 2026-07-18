import sys
from pathlib import Path

import grpc

from app import config
from app.logger import debug_log

GENERATED_DIR = Path(__file__).parent / "generated"
if str(GENERATED_DIR) not in sys.path:
    sys.path.insert(0, str(GENERATED_DIR))

try:
    import openviking_context_pb2
    import openviking_context_pb2_grpc
except ImportError:
    openviking_context_pb2 = None
    openviking_context_pb2_grpc = None


class ContextClient:
    def __init__(self):
        self._stub = None

    def _get_stub(self):
        if self._stub is None:
            channel = grpc.insecure_channel(config.OPENVIKING_CONTEXT_TARGET)
            self._stub = openviking_context_pb2_grpc.OpenVikingContextStub(channel)
        return self._stub

    def search_context(
        self,
        user_id: str,
        session_id: str,
        query: str,
        agent_id: str = "main",
        top_k: int = 8,
        max_tokens: int = 3000,
        max_messages: int = 6,
        commit_limit: int = 0,
    ):
        if openviking_context_pb2 is None:
            raise RuntimeError("openviking_context protobuf is not generated")

        try:
            request = openviking_context_pb2.SearchContextRequest(
                user_id=user_id,
                session_id=session_id,
                agent_id=agent_id or "main",
                query=query,
                top_k=top_k,
                max_tokens=max_tokens,
                max_messages=max_messages,
                commit_limit=commit_limit,
            )
            response = self._get_stub().SearchContext(
                request,
                timeout=config.CONTEXT_TIMEOUT_SECONDS,
            )

            if getattr(response, "error", ""):
                debug_log(f"SearchContext returned error: {response.error}")

            messages = []
            if response.session_summary:
                messages.append({
                    "role": "system",
                    "content": f"【会话摘要】\n{response.session_summary}",
                })
            for memory in response.memories:
                messages.append({
                    "role": "system",
                    "content": f"【相关记忆】{memory.content}",
                })
            for msg in response.recent_messages:
                messages.append({
                    "role": msg.role,
                    "content": msg.content,
                })
            return messages
        except Exception as exc:
            debug_log(f"SearchContext failed: {exc}")
            return []

    def append_turn(
        self,
        user_id: str,
        session_id: str,
        task_id: str,
        user_message: str,
        assistant_message: str,
        agent_id: str = "main",
        tool_summaries=None,
        commit_limit: int = 0,
    ):
        if openviking_context_pb2 is None:
            raise RuntimeError("openviking_context protobuf is not generated")

        try:
            request = openviking_context_pb2.AppendTurnRequest(
                user_id=user_id,
                session_id=session_id,
                agent_id=agent_id or "main",
                task_id=task_id,
                user_message=user_message,
                assistant_message=assistant_message,
                tool_summaries=tool_summaries or [],
                metadata={},
                commit_limit=commit_limit,
            )
            response = self._get_stub().AppendTurn(request, timeout=config.CONTEXT_TIMEOUT_SECONDS)
            if not response.ok:
                debug_log(f"AppendTurn returned ok=false: {response.error}")
            return bool(response.ok)
        except Exception as exc:
            debug_log(f"AppendTurn failed: {exc}")
            return False

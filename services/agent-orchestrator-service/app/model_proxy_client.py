import sys
import uuid
from pathlib import Path

import grpc

from app import config
from app.logger import debug_log

GENERATED_DIR = Path(__file__).parent / "generated"
if str(GENERATED_DIR) not in sys.path:
    sys.path.insert(0, str(GENERATED_DIR))

try:
    import model_proxy_pb2
    import model_proxy_pb2_grpc
except ImportError:
    model_proxy_pb2 = None
    model_proxy_pb2_grpc = None


class ModelProxyClient:
    def __init__(self):
        self._stub = None

    def _get_stub(self):
        if self._stub is None:
            # 图片以 base64 data URL 传输，放宽 gRPC 单条消息大小上限（128 MiB）
            max_msg_bytes = 128 * 1024 * 1024
            channel = grpc.insecure_channel(
                config.MODEL_PROXY_TARGET,
                options=[
                    ("grpc.max_send_message_length", max_msg_bytes),
                    ("grpc.max_receive_message_length", max_msg_bytes),
                ],
            )
            self._stub = model_proxy_pb2_grpc.ModelProxyStub(channel)
        return self._stub

    def chat_completion(self, task_id: str, agent_id: str, model_profile: str, messages: list[dict], params: dict):
        if model_proxy_pb2 is None:
            raise RuntimeError("model_proxy protobuf is not generated")

        try:
            request = model_proxy_pb2.ChatCompletionRequest(
                request_id=f"model-call-{uuid.uuid4().hex[:12]}",
                task_id=task_id,
                agent_id=agent_id,
                model_profile=model_profile,
                messages=[
                    model_proxy_pb2.Message(
                        role=m.get("role", "user"),
                        content=m.get("content", ""),
                        images=m.get("images") or [],
                    )
                    for m in messages
                ],
                params={str(k): str(v) for k, v in params.items()},
            )
            response = self._get_stub().ChatCompletion(
                request,
                timeout=config.MODEL_TIMEOUT_SECONDS,
            )
            if not response.ok:
                raise RuntimeError(response.error or "model proxy returned ok=false")
            return {
                "text": response.text,
                "reasoning": response.reasoning,
                "usage": {
                    "prompt_tokens": response.usage.prompt_tokens,
                    "completion_tokens": response.usage.completion_tokens,
                },
                "provider": response.provider,
                "model": response.model,
            }
        except Exception as exc:
            debug_log(f"ChatCompletion failed: {exc}")
            raise

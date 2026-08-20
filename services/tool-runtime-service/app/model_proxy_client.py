"""model-proxy-service 的 gRPC 客户端（ChatCompletion + Embedding）。

供 fetch_tools 使用：
- 策略二（语义召回）：embedding() 把 query / 分块文本向量化
- 策略三（LLM 提取降级）：chat_completion() 逐块提取相关信息

依赖 app/generated/model_proxy_pb2*（由 scripts/gen_proto.sh 生成）。
"""

import sys
import uuid
from pathlib import Path

import grpc

from app import config
from app.logger import log

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
            # 文本向量/提取结果都不大，按 128 MiB 放宽单条消息上限
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

    def chat_completion(
        self,
        task_id: str,
        agent_id: str,
        model_profile: str,
        messages: list[dict],
        params: dict | None = None,
    ) -> str:
        """调用 model-proxy ChatCompletion，返回文本；失败抛异常（由调用方降级）。"""
        if model_proxy_pb2 is None:
            raise RuntimeError("model_proxy protobuf is not generated")

        request = model_proxy_pb2.ChatCompletionRequest(
            request_id=f"tool-chat-{uuid.uuid4().hex[:12]}",
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
            params={str(k): str(v) for k, v in (params or {}).items()},
        )
        response = self._get_stub().ChatCompletion(
            request,
            timeout=config.MODEL_TIMEOUT_SECONDS,
        )
        if not response.ok:
            raise RuntimeError(response.error or "model proxy returned ok=false")
        return response.text

    def embedding(
        self,
        task_id: str,
        agent_id: str,
        model_profile: str,
        texts: list[str],
        params: dict | None = None,
    ) -> list[dict]:
        """调用 model-proxy Embedding，返回 [{index, vector}]；失败抛异常。"""
        if model_proxy_pb2 is None:
            raise RuntimeError("model_proxy protobuf is not generated")

        request = model_proxy_pb2.EmbeddingRequest(
            request_id=f"tool-embed-{uuid.uuid4().hex[:12]}",
            task_id=task_id,
            agent_id=agent_id,
            model_profile=model_profile,
            texts=[str(t) for t in (texts or [])],
            params={str(k): str(v) for k, v in (params or {}).items()},
        )
        response = self._get_stub().Embedding(
            request,
            timeout=config.MODEL_TIMEOUT_SECONDS,
        )
        if not response.ok:
            raise RuntimeError(response.error or "model proxy returned ok=false")
        return [
            {"index": item.index, "vector": list(item.vector)}
            for item in response.embeddings
        ]


model_proxy_client = ModelProxyClient()

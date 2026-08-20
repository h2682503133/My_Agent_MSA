import sys
from concurrent import futures
from pathlib import Path

import grpc

from app import config
from app.logger import log
from app.model_profiles import profile_store
from app.provider_client import provider_client

GENERATED_DIR = Path(__file__).parent / "generated"
if str(GENERATED_DIR) not in sys.path:
    sys.path.insert(0, str(GENERATED_DIR))

try:
    import model_proxy_pb2
    import model_proxy_pb2_grpc
except ImportError as exc:
    raise RuntimeError("gRPC generated files not found. Run bash scripts/gen_proto.sh first.") from exc


class ModelProxyService(model_proxy_pb2_grpc.ModelProxyServicer):
    def ChatCompletion(self, request, context):
        try:
            profile = profile_store.get_profile(request.model_profile)
            messages = [self._to_provider_message(msg) for msg in request.messages]

            result = provider_client.chat_completion(
                profile=profile,
                messages=messages,
                params=dict(request.params),
            )

            import json  # 确保已导入

            # 成功日志：完整原始结构体（无截断、无重排）
            log_msg = (
                f"ChatCompletion model={result.get('model', 'unknown')} "
                f"prompt={result.get('prompt_tokens', 0)} completion={result.get('completion_tokens', 0)}\n"
                f"  INPUT (raw):\n{json.dumps(messages, indent=2, ensure_ascii=False)}\n"
                f"  OUTPUT (raw):\n{json.dumps(result, indent=2, ensure_ascii=False)}"
            )

            # 如果 result 中有 reasoning_content 且不在顶层（或想单独强调），也可保留，但 json.dumps 已经包含
            log(log_msg)

            return model_proxy_pb2.ChatCompletionResponse(
                ok=True,
                text=result["text"],
                usage=model_proxy_pb2.Usage(
                    prompt_tokens=result.get("prompt_tokens", 0),
                    completion_tokens=result.get("completion_tokens", 0),
                ),
                provider=result.get("provider", ""),
                model=result.get("model", ""),
                error="",
            )

        except Exception as exc:
            log(
                "ChatCompletion failed "
                f"request_id={request.request_id} task_id={request.task_id} "
                f"agent_id={request.agent_id} profile={request.model_profile}: {exc}"
            )
            return model_proxy_pb2.ChatCompletionResponse(
                ok=False,
                text="",
                usage=model_proxy_pb2.Usage(),
                provider="",
                model="",
                error=str(exc),
            )

    @staticmethod
    def _to_provider_message(msg) -> dict:
        """单条消息转为 provider 消息体。

        无图片时保持纯文本 {role, content}；
        带图片时构建 OpenAI 多模态 content 数组：
        [{"type": "text", "text": ...}, {"type": "image_url", "image_url": {"url": ...}}]
        """
        base = {"role": msg.role}
        images = list(msg.images or [])
        if not images:
            base["content"] = msg.content
            return base

        content: list[dict] = [{"type": "text", "text": msg.content or ""}]
        for url in images:
            if not url:
                continue
            content.append({"type": "image_url", "image_url": {"url": url}})
        base["content"] = content
        return base

    def Embedding(self, request, context):
        try:
            profile = profile_store.get_profile(request.model_profile)
            result = provider_client.embedding(
                profile=profile,
                texts=list(request.texts),
                params=dict(request.params),
            )

            log(
                f"Embedding model={result.get('model', 'unknown')} "
                f"texts={len(request.texts)} "
                f"request_id={request.request_id} task_id={request.task_id} "
                f"agent_id={request.agent_id} profile={request.model_profile}"
            )

            return model_proxy_pb2.EmbeddingResponse(
                ok=True,
                embeddings=[
                    model_proxy_pb2.EmbeddingItem(
                        index=item["index"],
                        vector=item["vector"],
                    )
                    for item in result["embeddings"]
                ],
                provider=result.get("provider", ""),
                model=result.get("model", ""),
                error="",
            )

        except Exception as exc:
            log(
                "Embedding failed "
                f"request_id={request.request_id} task_id={request.task_id} "
                f"agent_id={request.agent_id} profile={request.model_profile}: {exc}"
            )
            return model_proxy_pb2.EmbeddingResponse(
                ok=False,
                embeddings=[],
                provider="",
                model="",
                error=str(exc),
            )


def serve():
    # 图片以 base64 data URL 传输，放宽 gRPC 单条消息大小上限（128 MiB，纯字节数限制）
    max_msg_bytes = 128 * 1024 * 1024
    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=32),
        options=[
            ("grpc.max_send_message_length", max_msg_bytes),
            ("grpc.max_receive_message_length", max_msg_bytes),
        ],
    )
    model_proxy_pb2_grpc.add_ModelProxyServicer_to_server(
        ModelProxyService(),
        server,
    )

    listen_addr = f"[::]:{config.MODEL_PROXY_GRPC_PORT}"
    server.add_insecure_port(listen_addr)
    server.start()
    log(f"model-proxy-service started on {listen_addr}")
    log(f"model profiles: {config.MODEL_PROFILES_PATH}")
    server.wait_for_termination()

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
                reasoning=result.get("reasoning", ""),
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

    @staticmethod
    def _resolve_image_profile(name: str) -> tuple[str, dict]:
        """解析 image profile：精确 → 连字符/下划线互换 → 任意 kind=image → 报错。"""
        candidates: list[str] = []
        for variant in (name, name.replace("_", "-"), name.replace("-", "_")):
            if variant and variant not in candidates:
                candidates.append(variant)

        for candidate in candidates:
            try:
                return candidate, profile_store.get_profile(candidate)
            except KeyError:
                continue

        # 兜底：配置里只要有一个 kind=image 的条目就用它（并告诉调用方实际用了哪个）
        try:
            data = profile_store.load()
        except Exception:  # noqa: BLE001
            data = {}
        models = data.get("models") if isinstance(data, dict) else None
        if isinstance(models, dict):
            image_names = [
                key for key, value in models.items()
                if isinstance(value, dict) and str(value.get("kind", "")).lower() == "image"
            ]
            if image_names:
                chosen = image_names[0]
                return chosen, profile_store.get_profile(chosen)
            raise KeyError(
                f"没有找到生图模型配置（{name}）；model_list.json 里需要一条 kind=image 的条目。"
                f"当前可用：{sorted(models)}"
            )
        raise KeyError(f"model profile not found: {name}")

    def ImageGeneration(self, request, context):
        """生图（model_list.json 里配置 kind=image 的条目即可，无需新页面）。"""
        try:
            profile_name, profile = self._resolve_image_profile(request.model_profile)
            # 限流按 profile 名分别计数（用解析后的真实名，别名不会各算一套）
            profile["_profile_name"] = profile_name
            if profile_name != request.model_profile:
                log(f"ImageGeneration profile 归一：{request.model_profile} -> {profile_name}")
            result = provider_client.image_generation(
                profile=profile,
                prompt=request.prompt,
                size=request.size,
                n=int(request.n or 1),
                params=dict(request.params),
            )
            log(
                f"ImageGeneration model={result.get('model', '')} "
                f"profile={request.model_profile} n={len(result.get('urls') or [])} "
                f"error={result.get('error', '')}"
            )
            if result.get("error"):
                return model_proxy_pb2.ImageGenerationResponse(
                    ok=False,
                    urls=[],
                    provider=result.get("provider", ""),
                    model=result.get("model", ""),
                    error=result["error"],
                )
            return model_proxy_pb2.ImageGenerationResponse(
                ok=True,
                urls=list(result.get("urls") or []),
                provider=result.get("provider", ""),
                model=result.get("model", ""),
                error="",
            )
        except Exception as exc:  # noqa: BLE001 - 显式失败，别静默
            log(f"ImageGeneration failed: {exc}")
            return model_proxy_pb2.ImageGenerationResponse(ok=False, urls=[], error=str(exc))

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

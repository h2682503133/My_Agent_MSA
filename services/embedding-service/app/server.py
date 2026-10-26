"""向量计算服务 gRPC 入口。

对外只暴露两件事：`Embed`（算向量）与 `Ping`（报告 provider/模型/维度）。
调用方拿到的是纯向量，不感知底层 provider —— 这就是"统一收束口"。
"""

import sys
from concurrent import futures
from pathlib import Path

import grpc

from app import config
from app.logger import debug_log, log
from app.provider import ProviderError, embed

GENERATED_DIR = Path(__file__).parent / "generated"
if str(GENERATED_DIR) not in sys.path:
    sys.path.insert(0, str(GENERATED_DIR))

try:
    import embedding_pb2
    import embedding_pb2_grpc
except ImportError as exc:  # pragma: no cover - 构建期就会暴露
    raise RuntimeError("gRPC generated files not found. Run bash scripts/gen_proto.sh first.") from exc


class EmbeddingService(embedding_pb2_grpc.EmbeddingServiceServicer):
    def __init__(self) -> None:
        self._dimension = 0

    def Ping(self, request, context):
        return embedding_pb2.PingResponse(
            ok=True,
            provider=config.EMBEDDING_PROVIDER_NAME,
            model=config.EMBEDDING_MODEL,
            dimension=self._dimension,
            error="",
        )

    def Embed(self, request, context):
        texts = list(request.texts)
        if not texts:
            return embedding_pb2.EmbedResponse(
                ok=True,
                items=[],
                provider=config.EMBEDDING_PROVIDER_NAME,
                model=request.model or config.EMBEDDING_MODEL,
                dimension=self._dimension,
                error="",
            )

        try:
            provider, model, vectors = embed(texts, request.model)
        except ProviderError as exc:
            log(f"Embed 失败 request={request.request_id or '-'} texts={len(texts)}: {exc}")
            return embedding_pb2.EmbedResponse(
                ok=False,
                items=[],
                provider=config.EMBEDDING_PROVIDER_NAME,
                model=request.model or config.EMBEDDING_MODEL,
                dimension=self._dimension,
                error=str(exc),
            )
        except Exception as exc:  # noqa: BLE001 - 不把未知异常丢给调用方
            log(f"Embed 未预期异常 request={request.request_id or '-'}: {type(exc).__name__}: {exc}")
            return embedding_pb2.EmbedResponse(
                ok=False,
                items=[],
                provider=config.EMBEDDING_PROVIDER_NAME,
                model=request.model or config.EMBEDDING_MODEL,
                dimension=self._dimension,
                error=f"{type(exc).__name__}: {exc}",
            )

        dimension = len(vectors[0]) if vectors else 0
        if dimension:
            self._dimension = dimension
        debug_log(
            f"Embed ok request={request.request_id or '-'} texts={len(texts)} "
            f"model={model} dim={dimension}"
        )
        return embedding_pb2.EmbedResponse(
            ok=True,
            items=[
                embedding_pb2.EmbeddingItem(index=index, vector=vector)
                for index, vector in enumerate(vectors)
            ],
            provider=provider,
            model=model,
            dimension=dimension,
            error="",
        )


def serve() -> None:
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=8))
    embedding_pb2_grpc.add_EmbeddingServiceServicer_to_server(EmbeddingService(), server)
    listen_addr = f"[::]:{config.EMBEDDING_GRPC_PORT}"
    server.add_insecure_port(listen_addr)
    server.start()
    log(
        f"embedding-service started on {listen_addr} "
        f"provider={config.EMBEDDING_PROVIDER_NAME} model={config.EMBEDDING_MODEL}"
    )
    server.wait_for_termination()

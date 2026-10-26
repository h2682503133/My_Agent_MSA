"""向量计算服务配置。

所有 provider 相关的可变量都收敛在这里：业务侧（如 openviking-context-service 的会话召回）
只通过 gRPC 调本服务，不硬编码任何 ollama / OpenAI 地址。
"""

import os


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


# gRPC 端口
EMBEDDING_GRPC_PORT = _int_env("EMBEDDING_GRPC_PORT", 5303)

# provider：目前是 OpenAI 兼容的 /v1/embeddings（ollama、vLLM、各家 API 都是这个形状）
EMBEDDING_API_BASE = os.getenv(
    "EMBEDDING_API_BASE", "http://host.docker.internal:11434/v1/embeddings"
)
EMBEDDING_API_KEY = os.getenv("EMBEDDING_API_KEY", "ollama")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "qwen3-embedding:0.6b")
EMBEDDING_PROVIDER_NAME = os.getenv("EMBEDDING_PROVIDER_NAME", "openai-compatible")

# 超时与限额
EMBEDDING_TIMEOUT_SECONDS = _float_env("EMBEDDING_TIMEOUT_SECONDS", 30.0)
# 单次请求最多多少条文本（超过则拆分，避免一次性打爆 provider）
EMBEDDING_MAX_TEXTS_PER_CALL = _int_env("EMBEDDING_MAX_TEXTS_PER_CALL", 64)
# 单条文本最大字符数（超长截断；embedding 模型有上下文上限，宁可截也不报错）
EMBEDDING_MAX_TEXT_CHARS = _int_env("EMBEDDING_MAX_TEXT_CHARS", 4000)

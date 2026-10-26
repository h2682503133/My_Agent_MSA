"""embedding provider：OpenAI 兼容的 /v1/embeddings（ollama / vLLM / 各家 API 同形）。

收束点的意义：业务侧只调本服务的 gRPC，provider 换掉时只改这里的配置或实现，
调用方（会话召回、后续别的检索）完全不用动。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from app import config


class ProviderError(RuntimeError):
    """provider 侧失败（网络 / 非 200 / 返回结构不符）。"""


def _clip(text: str) -> str:
    """超长文本截断：embedding 模型有上下文上限，宁可截断也不要整批失败。"""
    value = str(text or "")
    limit = max(1, config.EMBEDDING_MAX_TEXT_CHARS)
    return value if len(value) <= limit else value[:limit]


def _chunks(items: list[str], size: int):
    step = max(1, size)
    for start in range(0, len(items), step):
        yield items[start:start + step]


def _request_once(texts: list[str], model: str) -> list[list[float]]:
    payload = json.dumps({"model": model, "input": texts}).encode("utf-8")
    request = urllib.request.Request(
        config.EMBEDDING_API_BASE,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {config.EMBEDDING_API_KEY}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=config.EMBEDDING_TIMEOUT_SECONDS) as response:
            body = json.loads(response.read().decode("utf-8", "ignore"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "ignore")[:300]
        except Exception:
            pass
        raise ProviderError(f"HTTP {exc.code}: {detail or exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise ProviderError(f"网络错误: {exc.reason}") from exc
    except Exception as exc:  # noqa: BLE001 - 兜底成可读错误
        raise ProviderError(f"{type(exc).__name__}: {exc}") from exc

    items = body.get("data")
    if not isinstance(items, list):
        raise ProviderError(f"返回结构不符，缺少 data: {str(body)[:200]}")
    ordered = sorted(items, key=lambda item: item.get("index", 0))
    vectors = [list(item.get("embedding") or []) for item in ordered]
    if len(vectors) != len(texts):
        raise ProviderError(f"条数不匹配: 期望 {len(texts)} 实得 {len(vectors)}")
    if any(not vector for vector in vectors):
        raise ProviderError("返回了空向量")
    return vectors


def embed(texts: list[str], model: str = "") -> tuple[str, str, list[list[float]]]:
    """把一批文本向量化，返回 (provider, model, vectors)。"""
    clean = [_clip(text) for text in texts]
    if not clean:
        return config.EMBEDDING_PROVIDER_NAME, model or config.EMBEDDING_MODEL, []

    use_model = (model or config.EMBEDDING_MODEL).strip() or config.EMBEDDING_MODEL
    vectors: list[list[float]] = []
    for batch in _chunks(clean, config.EMBEDDING_MAX_TEXTS_PER_CALL):
        vectors.extend(_request_once(batch, use_model))
    return config.EMBEDDING_PROVIDER_NAME, use_model, vectors

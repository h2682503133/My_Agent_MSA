"""未 commit 会话内容的向量召回（补上"窗口外、还没进长期记忆"的盲区）。

背景
----
OpenViking 只对 `memories / resource / skill` 建向量索引（`find` 的 context_type 只有
这三种），会话的 `messages.jsonl` **不建索引** —— 所以在 session 上 `find` 恒 0 命中。

而"未 commit"的消息里有一段盲区：`commit_limit`（默认 15 条）才压缩进长期记忆，
上下文窗口只取最近 `max_messages`（默认 6）条，中间的几条既不在窗口里、也没进长期记忆。

做法
----
1. 读 `sessions/<full_session_id>/messages.jsonl`（= 尚未 commit 的全部消息）
2. 去掉已经在窗口里的（按条数从尾部剔除，尾部对不上才退回按正文去重）
3. 向量化走 **embedding-service**（唯一收束口；本服务不硬编码 ollama / OpenAI 地址）
4. 余弦相似度排序取 top-k（带最小分数阈值、同文去重）

缓存
----
向量缓存 **按 user_id 分桶**（不混在一个池子里）：每桶独立容量、独立 FIFO 淘汰，
桶数量超上限时按 LRU 整桶淘汰。key 里带模型名，换模型自动失效。进程内缓存，重启即空。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import sys
import threading
from collections import OrderedDict
from pathlib import Path

from app import config
from app.logger import debug_log

# user_id → (cache_key → vector)
_USER_BUCKETS: "OrderedDict[str, OrderedDict[str, list[float]]]" = OrderedDict()
_CACHE_LOCK = threading.Lock()


# ── 缓存（按用户分桶）──────────────────────────────────────────────


def _bucket_for(user_id: str) -> "OrderedDict[str, list[float]]":
    """取（或新建）某个用户的缓存桶；桶数超限时淘汰最久未用的整桶。"""
    key = str(user_id or "")
    with _CACHE_LOCK:
        bucket = _USER_BUCKETS.get(key)
        if bucket is None:
            bucket = OrderedDict()
            _USER_BUCKETS[key] = bucket
        _USER_BUCKETS.move_to_end(key)
        overflow = len(_USER_BUCKETS) - max(1, config.SESSION_RECALL_CACHE_MAX_USERS)
        while overflow > 0:
            _USER_BUCKETS.popitem(last=False)
            overflow -= 1
        return bucket


def _cache_key(model: str, text: str) -> str:
    payload = f"{model}\u0000{text}"
    return hashlib.sha1(payload.encode("utf-8", "ignore")).hexdigest()


def _cache_get(user_id: str, model: str, text: str) -> list[float] | None:
    bucket = _bucket_for(user_id)
    key = _cache_key(model, text)
    with _CACHE_LOCK:
        return bucket.get(key)


def _cache_put(user_id: str, model: str, text: str, vector: list[float]) -> None:
    bucket = _bucket_for(user_id)
    key = _cache_key(model, text)
    with _CACHE_LOCK:
        bucket[key] = vector
        limit = max(1, config.SESSION_RECALL_CACHE_PER_USER)
        overflow = len(bucket) - limit
        while overflow > 0:
            bucket.popitem(last=False)   # FIFO：该用户最早写入的
            overflow -= 1


def cache_stats() -> dict[str, int]:
    """缓存现状（调试 / 自检用）：各用户条目数。"""
    with _CACHE_LOCK:
        return {user: len(bucket) for user, bucket in _USER_BUCKETS.items()}


def clear_cache() -> None:
    with _CACHE_LOCK:
        _USER_BUCKETS.clear()


# ── 工具函数 ──────────────────────────────────────────────────────


def _message_text(msg: dict) -> str:
    """从一条 jsonl 消息里取出正文（parts[*].text 拼接）。"""
    parts = msg.get("parts") or []
    chunks: list[str] = []
    for part in parts:
        if isinstance(part, dict):
            text = part.get("text")
            if text:
                chunks.append(str(text))
        elif isinstance(part, str):
            chunks.append(part)
    if not chunks:
        content = msg.get("content")
        if content:
            chunks.append(str(content))
    return "".join(chunks).strip()


def _cosine(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = 0.0
    norm_left = 0.0
    norm_right = 0.0
    for a, b in zip(left, right):
        dot += a * b
        norm_left += a * a
        norm_right += b * b
    if norm_left <= 0 or norm_right <= 0:
        return 0.0
    return dot / (math.sqrt(norm_left) * math.sqrt(norm_right))


def _normalize(text: str) -> str:
    """去掉说话人前缀与空白，便于跨来源比对同一条消息。"""
    value = str(text or "").strip()
    if value.startswith("<") and ">:" in value[:40]:
        value = value.split(">:", 1)[1].strip()
    return " ".join(value.split())


def _same_message(row: dict, window_message: dict) -> bool:
    """判断 jsonl 里的一行与窗口里的一条是否是同一条消息（容忍清洗/前缀差异）。"""
    left = _normalize(row.get("content") or "")
    right = _normalize(str(window_message.get("content") or ""))
    if not left or not right:
        return False
    if left == right:
        return True
    head = 24
    return left[:head] == right[:head]


# ── 向量计算（走 embedding-service）────────────────────────────────


def _embed_sync(texts: list[str], model: str) -> list[list[float]]:
    """调 embedding-service 的 gRPC（同步；由调用方丢线程池）。"""
    import grpc

    generated_dir = str(Path(__file__).parent / "generated")
    if generated_dir not in sys.path:
        sys.path.insert(0, generated_dir)
    import embedding_pb2
    import embedding_pb2_grpc

    with grpc.insecure_channel(config.EMBEDDING_SERVICE_TARGET) as channel:
        stub = embedding_pb2_grpc.EmbeddingServiceStub(channel)
        response = stub.Embed(
            embedding_pb2.EmbedRequest(
                request_id="session-recall",
                texts=texts,
                model=model,
            ),
            timeout=config.EMBEDDING_TIMEOUT_SECONDS,
        )
    if not response.ok:
        raise RuntimeError(response.error or "embedding-service 返回失败")
    ordered = sorted(response.items, key=lambda item: item.index)
    return [list(item.vector) for item in ordered]


async def _embed(texts: list[str], model: str) -> list[list[float]]:
    return await asyncio.to_thread(_embed_sync, texts, model)


async def _resolve_vectors(user_id: str, texts: list[str], *, embed=None) -> list[list[float]]:
    """按该用户的缓存补齐向量；未命中的分批请求 embedding-service。"""
    model = config.EMBEDDING_MODEL
    vectors: list[list[float] | None] = [None] * len(texts)
    missing: list[tuple[int, str]] = []
    for index, text in enumerate(texts):
        cached = _cache_get(user_id, model, text)
        if cached is not None:
            vectors[index] = cached
        else:
            missing.append((index, text))

    # 冷启动/大批新消息时，单轮只补一部分（最老的优先），其余留给下一轮：
    # 候选按时间顺序排列，最老的等得最久、也最容易滑出窗口
    budget = max(1, config.SESSION_RECALL_EMBED_MAX_PER_CALL)
    if len(missing) > budget:
        debug_log(f"session recall: 本轮只补算 {budget}/{len(missing)} 条向量（其余下轮继续）")
        missing = missing[:budget]

    embed_fn = embed or _embed
    batch_size = max(1, config.SESSION_RECALL_EMBED_BATCH)
    for start in range(0, len(missing), batch_size):
        batch = missing[start:start + batch_size]
        try:
            embedded = await embed_fn([text for _, text in batch], model)
        except Exception as exc:
            # 召回是锦上添花：向量服务挂了就直接放弃，不影响本轮对话
            debug_log(f"session recall: 向量计算失败，跳过召回: {exc}")
            return [vector or [] for vector in vectors]
        for (index, text), vector in zip(batch, embedded):
            vectors[index] = vector
            _cache_put(user_id, model, text, vector)

    return [vector or [] for vector in vectors]


# ── 召回主流程 ────────────────────────────────────────────────────


async def recall(
    client,
    *,
    user_id: str,
    full_session_id: str,
    window_messages: list[dict],
    query: str,
    top_k: int,
    min_score: float,
    max_messages: int,
    maybe_await,
    embed=None,
) -> list[dict]:
    """对"未 commit 且不在窗口里"的会话消息做向量召回，返回与 memories 同构的条目。"""
    text_query = str(query or "").strip()
    if top_k <= 0 or not text_query:
        return []

    read_method = getattr(client, "read", None)
    if read_method is None:
        return []

    base_uri = f"viking://user/{user_id}/sessions/{full_session_id}"
    try:
        raw = await maybe_await(read_method(f"{base_uri}/messages.jsonl"))
    except Exception as exc:
        debug_log(f"session recall: 读取 messages.jsonl 失败: {exc}")
        return []

    payload = raw if isinstance(raw, str) else str(raw or "")
    rows: list[dict] = []
    for line in payload.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except Exception:
            continue
        text = _message_text(message)
        if not text:
            continue
        rows.append({
            "id": str(message.get("id") or ""),
            "role": str(message.get("role") or ""),
            "created_at": str(message.get("created_at") or ""),
            "content": text,
        })

    if not rows:
        return []

    # 去掉已经在上下文窗口里的部分。
    # 窗口是"按时间最新的 N 条"，所以优先**按条数从尾部剔除**；只有尾部对不上时
    # （窗口被 token 预算裁过 / 内容被清洗过）才退回按 role+正文去重。
    # 不能一律按正文去重：正文重复但时间不同的消息会被误删。
    window = list(window_messages or [])
    if window:
        tail = rows[-len(window):]
        if len(tail) == len(window) and _same_message(tail[-1], window[-1]):
            candidates = rows[:-len(window)]
        else:
            window_keys = {
                (str(item.get("role") or ""), _normalize(str(item.get("content") or "")))
                for item in window
            }
            candidates = [
                row for row in rows
                if (row["role"], _normalize(row["content"])) not in window_keys
            ]
    else:
        candidates = list(rows)

    if not candidates:
        return []
    if max_messages > 0 and len(candidates) > max_messages:
        candidates = candidates[-max_messages:]

    texts = [text_query] + [row["content"] for row in candidates]
    vectors = await _resolve_vectors(user_id, texts, embed=embed)
    query_vector = vectors[0]
    if not query_vector:
        return []

    scored: list[tuple[float, dict]] = []
    for row, vector in zip(candidates, vectors[1:]):
        score = _cosine(query_vector, vector)
        if score >= min_score:
            scored.append((score, row))
    scored.sort(key=lambda item: item[0], reverse=True)

    # 同一条正文（重复发送）只留一条：保留最高分、取最早那次的时间
    best: dict[str, tuple[float, dict]] = {}
    for score, row in scored:
        key = _normalize(row["content"])
        current = best.get(key)
        if current is None:
            best[key] = (score, row)
        else:
            kept_score, kept_row = current
            earliest = (
                row
                if (row.get("created_at") or "") < (kept_row.get("created_at") or "")
                else kept_row
            )
            best[key] = (max(score, kept_score), earliest)
    deduped = sorted(best.values(), key=lambda item: item[0], reverse=True)

    hits: list[dict] = []
    for score, row in deduped[:top_k]:
        stamp = ""
        created = row.get("created_at") or ""
        if created:
            try:
                import datetime

                moment = datetime.datetime.fromisoformat(created.replace("Z", "+00:00"))
                if moment.tzinfo is None:
                    moment = moment.replace(tzinfo=datetime.timezone.utc)
                stamp = moment.astimezone().strftime("[%m-%d %H:%M] ")
            except Exception:
                stamp = ""
        hits.append({
            "memory_id": f"session:{row.get('id') or ''}",
            "content": stamp + row["content"],
            "score": round(float(score), 4),
            "token_count": 0,
        })

    if hits:
        debug_log(
            f"session recall: {len(hits)}/{len(candidates)} 条命中"
            f"（top score={hits[0]['score']}） session={full_session_id} user={user_id}"
        )
    return hits

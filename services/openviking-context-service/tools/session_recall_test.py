"""session_recall 自检：缓存按用户分桶 + 召回主流程。

跑法：`python tools/session_recall_test.py`（工作目录 = 服务根，无需 grpc —— 向量计算用假函数注入）
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

SERVICE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE_ROOT))

from app import config, session_recall  # noqa: E402

PASSED = 0
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASSED
    if condition:
        PASSED += 1
        print(f"  ok    {name}")
    else:
        FAILED.append(name)
        print(f"  FAIL  {name}" + (f"   [{detail}]" if detail else ""))


# ── 假件 ──────────────────────────────────────────────────────────


def fake_vector(text: str) -> list[float]:
    """按关键词给一个确定的二维向量，便于断言相似度。"""
    if "杭州" in text:
        return [1.0, 0.0, 0.0]
    if "猫粮" in text:
        return [0.0, 1.0, 0.0]
    return [0.0, 0.0, 1.0]


async def fake_embed(texts: list[str], model: str) -> list[list[float]]:
    fake_embed.calls.append((list(texts), model))
    return [fake_vector(text) for text in texts]


fake_embed.calls = []


class FakeClient:
    """只实现 recall 用到的 read()。"""

    def __init__(self, messages: list[dict]) -> None:
        self._messages = messages

    async def read(self, uri: str) -> str:
        return "\n".join(json.dumps(m, ensure_ascii=False) for m in self._messages) + "\n"


async def maybe_await(value):
    return await value


def make_message(index: int, role: str, text: str, hour: int = 4) -> dict:
    return {
        "id": f"msg_{index:02d}",
        "role": role,
        "parts": [{"type": "text", "text": text}],
        "created_at": f"2026-10-26T{hour:02d}:{index * 2 % 60:02d}:00+00:00",
    }


async def run_recall(messages: list[dict], window: list[dict], query: str, **overrides) -> list[dict]:
    saved = {key: getattr(config, key) for key in overrides}
    try:
        for key, value in overrides.items():
            setattr(config, key, value)
        return await session_recall.recall(
            FakeClient(messages),
            user_id=overrides.pop("_user", "u1") if "_user" in overrides else "u1",
            full_session_id="test_session",
            window_messages=window,
            query=query,
            top_k=3,
            min_score=0.45,
            max_messages=40,
            maybe_await=maybe_await,
            embed=fake_embed,
        )
    finally:
        for key, value in saved.items():
            setattr(config, key, value)


# ── 1. 缓存按用户分桶 ─────────────────────────────────────────────


def test_cache_isolation() -> None:
    session_recall.clear_cache()
    session_recall._cache_put("alice", "m1", "同一句话", [1.0, 0.0])
    session_recall._cache_put("bob", "m1", "同一句话", [0.0, 1.0])

    stats = session_recall.cache_stats()
    check("两个用户各自一个桶", set(stats) == {"alice", "bob"}, str(stats))
    check("每桶各 1 条", stats.get("alice") == 1 and stats.get("bob") == 1, str(stats))
    check(
        "同一句话在不同用户下互不覆盖",
        session_recall._cache_get("alice", "m1", "同一句话") == [1.0, 0.0]
        and session_recall._cache_get("bob", "m1", "同一句话") == [0.0, 1.0],
    )


def test_cache_per_user_eviction() -> None:
    session_recall.clear_cache()
    saved = config.SESSION_RECALL_CACHE_PER_USER
    config.SESSION_RECALL_CACHE_PER_USER = 2
    try:
        for text in ("一", "二", "三"):
            session_recall._cache_put("alice", "m1", text, [1.0, 0.0])
        session_recall._cache_put("bob", "m1", "无关键", [0.0, 1.0])

        alice = session_recall.cache_stats()["alice"]
        check("超限后该用户只保留 2 条", alice == 2, str(alice))
        check("最早写入的被淘汰", session_recall._cache_get("alice", "m1", "一") is None)
        check("较新的仍在", session_recall._cache_get("alice", "m1", "三") is not None)
        check("别的用户不受影响", session_recall._cache_get("bob", "m1", "无关键") is not None)
    finally:
        config.SESSION_RECALL_CACHE_PER_USER = saved


def test_user_lru_eviction() -> None:
    session_recall.clear_cache()
    saved = config.SESSION_RECALL_CACHE_MAX_USERS
    config.SESSION_RECALL_CACHE_MAX_USERS = 2
    try:
        session_recall._cache_put("alice", "m1", "a", [1.0, 0.0])
        session_recall._cache_put("bob", "m1", "b", [1.0, 0.0])
        session_recall._cache_get("alice", "m1", "a")  # alice 变最近使用
        session_recall._cache_put("carol", "m1", "c", [1.0, 0.0])

        stats = session_recall.cache_stats()
        check("用户数不超过上限", len(stats) == 2, str(stats))
        check("最久未用的用户被整桶淘汰", "bob" not in stats, str(stats))
        check("刚用过的用户保留", "alice" in stats and "carol" in stats, str(stats))
    finally:
        config.SESSION_RECALL_CACHE_MAX_USERS = saved


def test_cache_key_includes_model() -> None:
    session_recall.clear_cache()
    saved = config.EMBEDDING_MODEL
    try:
        config.EMBEDDING_MODEL = "model-a"
        session_recall._cache_put("alice", "model-a", "同一句", [1.0, 0.0])
        config.EMBEDDING_MODEL = "model-b"
        check("换模型后旧向量不再命中", session_recall._cache_get("alice", "model-b", "同一句") is None)
        check("原模型仍命中", session_recall._cache_get("alice", "model-a", "同一句") == [1.0, 0.0])
    finally:
        config.EMBEDDING_MODEL = saved


# ── 2. 召回主流程 ─────────────────────────────────────────────────


def build_gap_messages() -> tuple[list[dict], list[dict]]:
    """10 条未 commit：前 6 条关键（杭州/猫粮…），后 4 条作为窗口。"""
    body = [
        make_message(0, "user", "我下周要去杭州出差三天"),
        make_message(1, "assistant", "好的，杭州记下了"),
        make_message(2, "user", "我的猫粮快没了"),
        make_message(3, "assistant", "皇家和渴望可以对比"),
        make_message(4, "user", "今天天气不错"),
        make_message(5, "assistant", "适合出门走走"),
    ]
    window_body = [
        make_message(6, "user", "晚上吃什么"),
        make_message(7, "assistant", "随便炒个菜"),
        make_message(8, "user", "那就这样"),
        make_message(9, "assistant", "好，晚安"),
    ]
    window = [
        {"role": m["role"], "content": m["parts"][0]["text"]} for m in window_body
    ]
    return body + window_body, window


def test_recall_hits() -> None:
    session_recall.clear_cache()
    fake_embed.calls.clear()
    messages, window = build_gap_messages()

    hits = asyncio.run(run_recall(messages, window, "杭州出差住哪儿"))
    check("命中窗口外的杭州消息", hits and "杭州出差三天" in hits[0]["content"], str(hits))
    check("命中的都是杭州相关", all("杭州" in h["content"] for h in hits), str([h["content"][:16] for h in hits]))
    check("不含窗口内的消息", all("晚上吃什么" not in h["content"] and "晚安" not in h["content"] for h in hits))
    check("带时间戳前缀", hits and hits[0]["content"].startswith("["), str(hits[:1]))
    check("带 session 前缀的 memory_id", hits and hits[0]["memory_id"].startswith("session:"))
    check("分数达到阈值", hits and hits[0]["score"] >= 0.45, str(hits))

    hits2 = asyncio.run(run_recall(messages, window, "猫粮买什么牌子"))
    check("换成猫粮也能命中对应消息", hits2 and "猫粮" in hits2[0]["content"], str(hits2[:1]))


def test_recall_second_turn_uses_cache() -> None:
    session_recall.clear_cache()
    fake_embed.calls.clear()
    messages, window = build_gap_messages()

    asyncio.run(run_recall(messages, window, "杭州出差住哪儿"))
    first_calls = len(fake_embed.calls)
    asyncio.run(run_recall(messages, window, "杭州出差住哪儿"))
    second_calls = len(fake_embed.calls) - first_calls

    check("第一轮算了向量", first_calls >= 1, str(first_calls))
    check("第二轮只算未命中的（0 次）", second_calls == 0, str(second_calls))


def test_recall_dedupes_repeated_text() -> None:
    session_recall.clear_cache()
    messages = [
        make_message(0, "user", "我的猫粮快没了"),
        make_message(1, "user", "我的猫粮快没了"),
        make_message(2, "user", "我的猫粮快没了"),
        make_message(3, "user", "晚上吃什么"),
    ]
    hits = asyncio.run(run_recall(messages, [], "猫粮"))
    check("重复正文只留一条", len(hits) == 1, str(len(hits)))


def test_recall_respects_embed_budget() -> None:
    session_recall.clear_cache()
    fake_embed.calls.clear()
    messages = [make_message(i, "user", f"第{i}条无关内容") for i in range(6)]
    asyncio.run(
        run_recall(messages, [], "杭州", SESSION_RECALL_EMBED_MAX_PER_CALL=2, SESSION_RECALL_EMBED_BATCH=16)
    )
    total_texts = sum(len(texts) for texts, _ in fake_embed.calls)
    check("单轮补算条数受限（≤2）", total_texts <= 2, f"实际 {total_texts}")


def test_recall_skips_when_no_gap() -> None:
    session_recall.clear_cache()
    fake_embed.calls.clear()
    messages = [make_message(i, "user", f"第{i}条") for i in range(3)]
    window = [{"role": "user", "content": "第0条"},
              {"role": "user", "content": "第1条"},
              {"role": "user", "content": "第2条"}]
    hits = asyncio.run(run_recall(messages, window, "随便问"))
    check("没有窗口外消息 → 不召回", hits == [], str(hits))
    check("也不调向量服务", fake_embed.calls == [], str(fake_embed.calls))


def main() -> int:
    print("缓存按用户分桶")
    test_cache_isolation()
    test_cache_per_user_eviction()
    test_user_lru_eviction()
    test_cache_key_includes_model()

    print("召回主流程")
    test_recall_hits()
    test_recall_second_turn_uses_cache()
    test_recall_dedupes_repeated_text()
    test_recall_respects_embed_budget()
    test_recall_skips_when_no_gap()

    print()
    if FAILED:
        print(f"FAILED {len(FAILED)}: {FAILED}")
        return 1
    print(f"ALL OK — {PASSED} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

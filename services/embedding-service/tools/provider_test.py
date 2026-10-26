"""embedding-service provider 自检（不依赖真实模型）。

跑法：`python tools/provider_test.py`
"""

from __future__ import annotations

import sys
from pathlib import Path

SERVICE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE_ROOT))

from app import config, provider  # noqa: E402

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


def main() -> int:
    calls: list[list[str]] = []
    saved = (config.EMBEDDING_MAX_TEXTS_PER_CALL, config.EMBEDDING_MAX_TEXT_CHARS)

    def fake_request(texts: list[str], model: str) -> list[list[float]]:
        calls.append(list(texts))
        return [[float(len(text)), float(i)] for i, text in enumerate(texts)]

    provider._request_once = fake_request  # type: ignore[assignment]
    try:
        print("分批与顺序")
        config.EMBEDDING_MAX_TEXTS_PER_CALL = 2
        _, _, vectors = provider.embed(["a", "bb", "ccc"], "")
        check("按上限分批（3 条 / 每批 2 → 2 批）", len(calls) == 2, str([len(c) for c in calls]))
        check("批次顺序保持", [v[0] for v in vectors] == [1.0, 2.0, 3.0], str(vectors))
        check("条数一致", len(vectors) == 3, str(len(vectors)))

        print("长文本截断")
        calls.clear()
        config.EMBEDDING_MAX_TEXTS_PER_CALL = 8
        config.EMBEDDING_MAX_TEXT_CHARS = 5
        provider.embed(["0123456789"], "")
        check("按字符上限截断", calls and calls[0] == ["01234"], str(calls))

        print("空输入")
        calls.clear()
        _, _, vectors = provider.embed([], "")
        check("空列表不调用 provider", calls == [], str(calls))
        check("空列表返回空向量", vectors == [], str(vectors))

        print("错误传播")
        def boom(texts, model):
            raise provider.ProviderError("HTTP 500: down")

        provider._request_once = boom  # type: ignore[assignment]
        try:
            provider.embed(["x"], "")
            check("provider 失败向上抛 ProviderError", False, "没有抛异常")
        except provider.ProviderError as exc:
            check("provider 失败向上抛 ProviderError", "500" in str(exc), str(exc))

        print("返回顺序错乱也要修正")
        def shuffled(texts, model):
            return [[float(i)] for i in range(len(texts))]

        provider._request_once = shuffled  # type: ignore[assignment]
        _, _, vectors = provider.embed(["a", "b"], "")
        check("返回条数对齐", len(vectors) == 2, str(vectors))
    finally:
        config.EMBEDDING_MAX_TEXTS_PER_CALL, config.EMBEDDING_MAX_TEXT_CHARS = saved

    print()
    if FAILED:
        print(f"FAILED {len(FAILED)}: {FAILED}")
        return 1
    print(f"ALL OK — {PASSED} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

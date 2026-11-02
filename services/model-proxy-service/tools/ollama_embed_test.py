"""Ollama embedding 返回格式兼容自检（离线：桩掉 HTTP 请求）。"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

SERVICE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVICE))

from app.provider_client import ProviderClient  # noqa: E402

FAILS: list[str] = []
CHECKS = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global CHECKS
    CHECKS += 1
    if cond:
        print(f"  ok    {name}")
    else:
        FAILS.append(name)
        print(f"  FAIL  {name} {extra}")


def make_client(payload: dict):
    client = ProviderClient()
    client._request = lambda method, url, headers, body: SimpleNamespace(json=lambda: payload)
    return client


PROFILE = {"provider": "ollama", "api_url": "http://x/api/embed", "model": "nomic-embed-text"}


def main() -> int:
    print("新版 /api/embed：embeddings 数组")
    client = make_client({"embeddings": [[0.1, 0.2], [0.3, 0.4]]})
    result = client.embedding(PROFILE, ["a", "b"], {})
    check("返回两条", len(result["embeddings"]) == 2, str(result))
    check("index 递增", [item["index"] for item in result["embeddings"]] == [0, 1])
    check("向量是 float", result["embeddings"][0]["vector"] == [0.1, 0.2])
    check("provider 回填", result["provider"] == "ollama")

    print("老版 /api/embeddings：单条 embedding")
    legacy = make_client({"embedding": [0.5, 0.6, 0.7]})
    result2 = legacy.embedding(PROFILE, ["only-one"], {})
    check("单条也能用", len(result2["embeddings"]) == 1)
    check("向量内容正确", result2["embeddings"][0]["vector"] == [0.5, 0.6, 0.7])

    print("老版端点 + 多条输入 → 明确报错（不静默丢数据）")
    try:
        legacy.embedding(PROFILE, ["a", "b"], {})
        check("多条输入应报错", False, "没有报错")
    except RuntimeError as exc:
        check("多条输入应报错", "/api/embed" in str(exc), str(exc)[:90])

    print("被拍平的一维数组也认")
    flat = make_client({"embeddings": [0.9, 0.8]})
    result3 = flat.embedding(PROFILE, ["a"], {})
    check("一维数组当作单条", result3["embeddings"][0]["vector"] == [0.9, 0.8])

    print("空返回 → 报错")
    empty = make_client({"embeddings": []})
    try:
        empty.embedding(PROFILE, ["a"], {})
        check("空返回应报错", False)
    except RuntimeError as exc:
        check("空返回应报错", "no vectors" in str(exc), str(exc)[:60])

    print()
    if FAILS:
        print(f"FAILED {len(FAILS)}: {FAILS}")
        return 1
    print(f"ALL OK — {CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

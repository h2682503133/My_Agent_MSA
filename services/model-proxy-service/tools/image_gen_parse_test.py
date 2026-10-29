"""生图返回解析自检（不联网：桩掉 provider_client._request）。

覆盖：
 1. OpenAI/ark 的 `{"data":[{"url":...}]}` → urls
 2. `{"data":[{"b64_json":...}]}` → data URL
 3. HTTP 4xx → error 显式返回
 4. 空返回 → error 明确说明
 5. model_params / 调用参数合并，size 覆盖
"""

import base64
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.provider_client import ProviderClient  # noqa: E402

FAILS = []
CHECKS = 0


def check(name, cond, extra=""):
    global CHECKS
    CHECKS += 1
    if cond:
        print(f"  ok    {name}")
    else:
        FAILS.append(name)
        print(f"  FAIL  {name} {extra}")


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)[:200]

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


PROFILE = {
    "provider": "openai_compatible",
    "api_url": "https://example.invalid/images/generations",
    "method": "POST",
    "model": "test-image-model",
    "api_key": "k",
    "model_params": {"response_format": "url", "n": 1},
}


def run(payload, status_code=200, size="", params=None, capture=None):
    client = ProviderClient()

    def fake_request(method, url, headers, body):
        if capture is not None:
            capture.update({"method": method, "url": url, "body": body})
        return FakeResponse(payload, status_code)

    client._request = fake_request  # type: ignore[assignment]
    return client.image_generation(PROFILE, "一只猫", size=size, n=1, params=params)


def main():
    print("url 形态")
    result = run({"data": [{"url": "https://cdn/x.png"}]})
    check("拿到 url", result["urls"] == ["https://cdn/x.png"], str(result))
    check("无错误", result["error"] == "", result["error"])

    print("b64 形态")
    raw = base64.b64encode(b"\x89PNG\r\n").decode()
    result = run({"data": [{"b64_json": raw}]})
    check("转成 data URL", result["urls"][0].startswith("data:image/png;base64,"), result["urls"][0][:40])

    print("多张")
    result = run({"data": [{"url": "https://cdn/a.png"}, {"url": "https://cdn/b.png"}]})
    check("两张都返回", len(result["urls"]) == 2, str(result["urls"]))

    print("HTTP 错误")
    result = run({"error": {"message": "quota exceeded"}}, status_code=429)
    check("ok=False", result["urls"] == [])
    check("错误里带状态码", "429" in result["error"] and "quota exceeded" in result["error"], result["error"])

    print("空返回")
    result = run({"data": []})
    check("空列表报错", "没返回图片" in result["error"], result["error"])

    print("参数合并")
    capture = {}
    run({"data": [{"url": "https://cdn/x.png"}]}, size="512x512", params={"quality": "hd"}, capture=capture)
    body = capture["body"]
    check("size 生效", body.get("size") == "512x512", str(body.get("size")))
    check("model_params 带入", body.get("response_format") == "url", str(body.get("response_format")))
    check("调用参数带入", body.get("quality") == "hd", str(body.get("quality")))
    check("model 字段", body.get("model") == "test-image-model", str(body.get("model")))
    check("POST 到 api_url", capture["method"] == "POST" and capture["url"].endswith("/images/generations"), str(capture))

    print()
    if FAILS:
        print(f"FAILED {len(FAILS)}: {FAILS}")
        return 1
    print(f"ALL OK — {CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

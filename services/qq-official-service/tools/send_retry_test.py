"""qq-official 发送重试自检（不联网：用假的 _request_once）。

覆盖：
  1. 瞬时错误（DNS/连不上）→ 重试后成功
  2. 业务错误（err_code 40034xxx / http 200）→ 不重试
  3. 连续瞬时错误 → 用尽重试后返回失败
  4. token_store._fetch 瞬时错误重试后拿到 token
  5. is_transient 判定边界
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import openapi_client as oc  # noqa: E402
from app import token_store as ts_mod  # noqa: E402

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


def run(coro):
    return asyncio.run(coro)


class FakeTokenStore:
    async def get(self, force: bool = False):
        return "FAKE_TOKEN"


class FakeClient(oc.OpenApiClient):
    """按脚本依次返回 _request_once 的结果。"""

    def __init__(self, script):
        super().__init__("https://example.invalid", FakeTokenStore())
        self.script = list(script)
        self.calls = 0

    async def _request_once(self, method, path, payload=None):
        self.calls += 1
        return self.script.pop(0) if self.script else {"err_code": 0, "id": "x", "http_status": 200}


TRANSIENT = {"err_code": -1, "message": "request failed: Cannot connect to host api.bot.qq.com:443 ssl:default [Temporary failure in name resolution]", "http_status": 0}
OK = {"err_code": 0, "id": "mid-1", "http_status": 200}
BIZ_ERR = {"err_code": 40034128, "message": "msg_id expired", "http_status": 200}


def main():
    print("is_transient 判定")
    check("DNS 抖动算瞬时", oc.is_transient(TRANSIENT) is True)
    check("业务错误不算瞬时", oc.is_transient(BIZ_ERR) is False)
    check("http 200 不算瞬时", oc.is_transient({"err_code": -1, "message": "x", "http_status": 200}) is False)
    check("空 dict 不算", oc.is_transient({}) is False)

    print("发送重试")
    client = FakeClient([TRANSIENT, OK])
    body = run(client._request("POST", "/v2/users/u/messages"))
    check("瞬时错误后重试成功", body.get("id") == "mid-1", str(body))
    check("一共调了 2 次", client.calls == 2, str(client.calls))

    client2 = FakeClient([BIZ_ERR])
    body2 = run(client2._request("POST", "/v2/users/u/messages"))
    check("业务错误不重试", client2.calls == 1 and body2.get("err_code") == 40034128, str(client2.calls))

    client3 = FakeClient([TRANSIENT, TRANSIENT, TRANSIENT, TRANSIENT])
    body3 = run(client3._request("POST", "/v2/users/u/messages"))
    check("连续瞬时错误会重试到上限", client3.calls == 3, str(client3.calls))
    check("仍失败时返回错误", body3.get("err_code") == -1, str(body3))

    print("token 重试")
    store = ts_mod.TokenStore("https://example.invalid", "appid", "secret")
    script = [({}, "Cannot connect to host api.bot.qq.com:443 [Temporary failure in name resolution]"),
              ({}, "Cannot connect to host api.bot.qq.com:443"),
              ({"access_token": "TK", "expires_in": 7200}, "")]
    calls = {"n": 0}

    async def fake_fetch_once():
        item = script[min(calls["n"], len(script) - 1)]
        calls["n"] += 1
        return item

    store._fetch_once = fake_fetch_once
    token, ttl = run(store._fetch())
    check("token 瞬时失败后重试成功", token == "TK" and ttl == 7200, f"{token} {ttl}")
    check("token 重试次数正确", calls["n"] == 3, str(calls["n"]))

    print("token 业务错误不重试")
    store2 = ts_mod.TokenStore("https://example.invalid", "appid", "bad")
    calls2 = {"n": 0}

    async def fake_fetch_once2():
        calls2["n"] += 1
        return ({"code": 100016, "message": "appid or secret invalid"}, "")

    store2._fetch_once = fake_fetch_once2
    token2, _ = run(store2._fetch())
    check("业务错误不重试", calls2["n"] == 1 and token2 == "", f"{calls2['n']} {token2}")

    print()
    if FAILS:
        print(f"FAILED {len(FAILS)}: {FAILS}")
        return 1
    print(f"ALL OK — {CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""QQ 官方 OpenAPI 客户端：网关地址 + 发送消息。

- 统一域名 https://api.bot.qq.com
- 鉴权头 Authorization: QQBot {ACCESS_TOKEN}
- 业务错误也返回 HTTP 200，必须以 body 的 err_code 判定成功与否
"""
import asyncio
import aiohttp

# 瞬时网络错误：DNS 抖动 / 连不上 / 超时 / 连接被断
_TRANSIENT_HINTS = (
    "name resolution",
    "cannot connect",
    "timeout",
    "timed out",
    "server disconnected",
    "connection reset",
    "connection aborted",
    "network is unreachable",
    "temporary failure",
)

# 重试退避（秒）；长度 = 重试次数
_RETRY_BACKOFF = (0.4, 1.2)


def is_transient(body: dict) -> bool:
    """这次失败是不是"网络抖动"（值得重试）。"""
    if not body:
        return False
    if body.get("http_status") not in (0, None):
        return False
    code = body.get("err_code")
    if code not in (-1, None):
        return False
    message = str(body.get("message") or "").lower()
    return any(hint in message for hint in _TRANSIENT_HINTS)


class OpenApiClient:
    def __init__(self, api_base: str, token_store, timeout_seconds: int = 10):
        self.api_base = api_base
        self.tokens = token_store
        self.timeout = aiohttp.ClientTimeout(total=timeout_seconds)

    async def _headers(self) -> dict:
        token = await self.tokens.get()
        return {
            "Authorization": f"QQBot {token}",
            "Content-Type": "application/json",
            "X-Union-Appid": "",
        }

    async def _request_once(self, method: str, path: str, payload: dict | None = None) -> dict:
        url = f"{self.api_base}{path}"
        headers = await self._headers()
        headers.pop("X-Union-Appid", None)
        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.request(method, url, json=payload, headers=headers) as resp:
                    try:
                        body = await resp.json(content_type=None)
                    except Exception:
                        body = {"err_code": -1, "message": await resp.text()}
                    if not isinstance(body, dict):
                        body = {"err_code": -1, "message": str(body)}
                    body.setdefault("http_status", resp.status)
                    return body
        except Exception as exc:
            return {"err_code": -1, "message": f"request failed: {exc}", "http_status": 0}

    async def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        """带瞬时错误重试的请求（DNS 抖动不要直接算失败）。"""
        body = await self._request_once(method, path, payload)
        for attempt, delay in enumerate(_RETRY_BACKOFF, start=1):
            if not is_transient(body):
                break
            print(
                f"[qq-official] 请求瞬时失败，重试 {attempt}/{len(_RETRY_BACKOFF)} "
                f"{method} {path} :: {str(body.get('message'))[:80]}",
                flush=True,
            )
            await asyncio.sleep(delay)
            body = await self._request_once(method, path, payload)
        return body

    async def get_gateway(self) -> tuple[str, int]:
        """GET /gateway/bot → (wss url, 建议分片数)。失败返回 ("", 0)。"""
        body = await self._request("GET", "/gateway/bot")
        url = str(body.get("url") or "")
        if not url:
            print(
                f"[qq-official] 获取网关失败 http={body.get('http_status')} "
                f"code={body.get('code')} message={body.get('message')}",
                flush=True,
            )
            return "", 0
        try:
            shards = int(body.get("shards") or 1)
        except (TypeError, ValueError):
            shards = 1
        return url, max(shards, 1)

    async def send_message(
        self,
        kind: str,
        target: str,
        content: str,
        msg_id: str = "",
        msg_seq: int | None = None,
    ) -> dict:
        """发文本消息。

        kind: "private" → /v2/users/{openid}/messages
              "group"   → /v2/groups/{group_openid}/messages
        带 msg_id 为被动回复（窗口内有效）；不带为主动消息（受频控）。
        """
        if kind == "private":
            path = f"/v2/users/{target}/messages"
        elif kind == "group":
            path = f"/v2/groups/{target}/messages"
        else:
            return {"ok": False, "err_code": -2, "message": f"unknown kind {kind}"}

        payload: dict = {"content": content, "msg_type": 0}
        if msg_id:
            payload["msg_id"] = msg_id
            payload["msg_seq"] = int(msg_seq or 1)

        body = await self._request("POST", path, payload)
        err = body.get("err_code")
        ok = body.get("id") is not None and (err in (None, 0, "0"))
        return {
            "ok": ok,
            "err_code": err,
            "message": body.get("message") or "",
            "id": body.get("id") or "",
            "http_status": body.get("http_status"),
        }

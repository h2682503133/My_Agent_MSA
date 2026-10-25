"""access_token 缓存：提前刷新，失败时保留旧值并重试。

官方接口 POST https://api.bot.qq.com/app/getAppAccessToken
请求体 {appId, clientSecret} → {access_token, expires_in}。
注意：业务失败也返回 HTTP 200，要看 body 里有没有 access_token。
"""
import asyncio
import time

import aiohttp


class TokenStore:
    def __init__(self, api_base: str, app_id: str, client_secret: str):
        self.api_base = api_base
        self.app_id = app_id
        self.client_secret = client_secret
        self._token = ""
        self._expire_at = 0.0
        self._lock = asyncio.Lock()

    @property
    def token(self) -> str:
        return self._token

    async def get(self, force: bool = False) -> str:
        async with self._lock:
            now = time.time()
            # 提前 60 秒刷新（官方文档：距过期 60 秒内可拿到新 token）
            if not force and self._token and now < self._expire_at - 60:
                return self._token
            token, ttl = await self._fetch()
            if token:
                self._token = token
                self._expire_at = now + ttl
                print(f"[qq-official] access_token 已刷新，ttl={ttl}s", flush=True)
            else:
                print("[qq-official] access_token 获取失败，沿用旧值", flush=True)
            return self._token

    async def _fetch(self) -> tuple[str, int]:
        url = f"{self.api_base}/app/getAppAccessToken"
        payload = {"appId": self.app_id, "clientSecret": self.client_secret}
        timeout = aiohttp.ClientTimeout(total=10)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, json=payload) as resp:
                    body = await resp.json(content_type=None)
        except Exception as exc:
            print(f"[qq-official] getAppAccessToken 请求异常: {exc}", flush=True)
            return "", 0

        if not isinstance(body, dict):
            return "", 0
        token = str(body.get("access_token") or "")
        if not token:
            # code 100007 appid 无效 / 100016 appid 或 secret 错误
            print(
                f"[qq-official] getAppAccessToken 失败 code={body.get('code')} "
                f"message={body.get('message')}",
                flush=True,
            )
            return "", 0
        try:
            ttl = int(str(body.get("expires_in") or "7200"))
        except (TypeError, ValueError):
            ttl = 7200
        return token, max(ttl, 120)

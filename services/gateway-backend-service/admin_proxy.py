"""管理面板反代：把后台服务收拢到 gateway 的入口上。

为什么放在 gateway 而不是前端 nginx：鉴权/日志/限流的收束口在 gateway，前端只该管静态页与聊天 SSE。

两种通道：

1. **前缀式**（5210，与主 API 同端口）：`/admin/<service>/<path>`
   → 适合**纯 API** 的服务（openviking / user-service / image-assets）。
2. **整站式**（独立端口）：每个「自带 HTML 且用绝对路径」的界面一个端口，
   避免和 gateway 自己的 `/api/*`、`/assets/*` 撞车：
   - 3080 → LLBot WebUI（`llbot:3080`）：资源 `/assets/*`、接口 `/api/*`
   - 5700 → 管理面板 dashboard-service（`dashboard-service:5700`）
   两者都是 Vite/静态页 + 绝对路径，前缀反代必然串到 gateway 的 `/api/*` 上，
   所以按端口整站反代最稳（一个进程内跑多个 uvicorn，见 serve.py）。
"""
from __future__ import annotations

import os

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask

# 服务名 → 内网地址（都可被同名环境变量覆盖，便于本地/其它集群）
PROXY_TARGETS: dict[str, str] = {
    "dashboard": os.getenv("ADMIN_DASHBOARD_TARGET", "http://dashboard-service:5700"),
    "user": os.getenv("ADMIN_USER_TARGET", "http://user-service:5204"),
    "assets": os.getenv("ADMIN_ASSETS_TARGET", "http://image-assets-service:80"),
    "openviking": os.getenv("ADMIN_OPENVIKING_TARGET", "http://openviking:1933"),
}

# 整站反代（独立端口）：端口 → (服务名, 目标地址, 环境变量)
# 端口号**沿用各服务原来的**（3080 LLBot WebUI / 5700 管理面板），
# 只是由 gateway 这个容器监听并整站反代 —— 外面访问的端口不变。
PASSTHROUGH_PORTS: dict[int, tuple[str, str]] = {
    3080: ("llbot", os.getenv("LLBOT_WEBUI_TARGET", "http://llbot:3080")),
    5700: ("dashboard", PROXY_TARGETS["dashboard"]),
}

# 逐跳头：不能透传（RFC 7230）
_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
}

_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]


def _client() -> httpx.AsyncClient:
    # read=None：SSE / 长轮询不吃默认超时
    return httpx.AsyncClient(timeout=httpx.Timeout(connect=10.0, read=None, write=60.0, pool=10.0))


async def _forward(request: Request, base_url: str, path: str) -> StreamingResponse | JSONResponse:
    target = f"{base_url.rstrip('/')}/{path.lstrip('/')}" if path else f"{base_url.rstrip('/')}/"
    headers = {
        name: value for name, value in request.headers.items() if name.lower() not in _HOP_HEADERS
    }
    client = _client()
    try:
        upstream_request = client.build_request(
            request.method,
            target,
            headers=headers,
            content=request.stream(),
            params=request.query_params,
        )
        upstream = await client.send(upstream_request, stream=True)
    except Exception as exc:  # noqa: BLE001 - 上游不可达要如实回报
        await client.aclose()
        return JSONResponse(
            status_code=502,
            content={"error": f"admin proxy 上游不可达: {target}", "detail": str(exc)},
        )

    response_headers = {
        name: value
        for name, value in upstream.headers.items()
        if name.lower() not in _HOP_HEADERS and name.lower() != "content-length"
    }

    async def _close() -> None:
        await upstream.aclose()
        await client.aclose()

    return StreamingResponse(
        upstream.aiter_raw(),
        status_code=upstream.status_code,
        headers=response_headers,
        background=BackgroundTask(_close),
    )


def _unknown(service: str) -> JSONResponse:
    return JSONResponse(
        status_code=404,
        content={"error": f"未知的管理服务 {service!r}", "services": sorted(PROXY_TARGETS)},
    )


def register_admin_proxy(app: FastAPI) -> None:
    """把 `/admin/<service>/…` 挂到 gateway 主应用上（前缀式，纯 API 服务）。"""

    @app.api_route("/admin/{service}/{path:path}", methods=_METHODS, include_in_schema=False)
    async def admin_proxy(service: str, path: str, request: Request):
        base = PROXY_TARGETS.get(service.lower())
        if base is None:
            return _unknown(service)
        return await _forward(request, base, path)

    @app.api_route("/admin/{service}", methods=_METHODS, include_in_schema=False)
    async def admin_proxy_root(service: str, request: Request):
        base = PROXY_TARGETS.get(service.lower())
        if base is None:
            return _unknown(service)
        return await _forward(request, base, "")


def _build_passthrough_app(name: str, base_url: str) -> FastAPI:
    app = FastAPI(title=f"{name}-proxy", docs_url=None, redoc_url=None, openapi_url=None)

    @app.api_route("/{path:path}", methods=_METHODS, include_in_schema=False)
    async def passthrough(path: str, request: Request):
        return await _forward(request, base_url, path)

    return app


# 端口 3080/5700：整站反代（由 serve.py 一起拉起，端口沿用各自服务的原号）
PASSTHROUGH_APPS: dict[int, FastAPI] = {
    port: _build_passthrough_app(name, target)
    for port, (name, target) in PASSTHROUGH_PORTS.items()
}

# 兼容早期引用
llbot_proxy_app = PASSTHROUGH_APPS[3080]

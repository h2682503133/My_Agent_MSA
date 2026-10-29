"""gateway 进程内多端口启动器。

一个进程里跑多个 uvicorn：
- 5210 主 API（app:app）
- 3080 LLBot WebUI 整站反代（端口沿用原号）
- 5700 管理面板 dashboard-service 整站反代（端口沿用原号）

这样「管理面板那几个入口」都在 gateway 里，宿主一条 port-forward 即可全通。
"""
from __future__ import annotations

import asyncio

import uvicorn

import app as gateway_app
import admin_proxy


async def _serve_all() -> None:
    configs = [
        uvicorn.Config(gateway_app.app, host="0.0.0.0", port=5210, log_level="info"),
    ]
    for port, proxy_app in admin_proxy.PASSTHROUGH_APPS.items():
        configs.append(
            uvicorn.Config(proxy_app, host="0.0.0.0", port=port, log_level="info")
        )

    servers = [uvicorn.Server(config) for config in configs]
    for server in servers:
        server.install_signal_handlers = lambda: None  # 交给外层统一处理
    await asyncio.gather(*(server.serve() for server in servers))


def main() -> None:
    asyncio.run(_serve_all())


if __name__ == "__main__":
    main()

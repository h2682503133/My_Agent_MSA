"""宿主 MCP 中继：把只监听回环的 Playwright MCP（默认 ::1:9333）暴露给容器网络。

链路：
  k8s pod → 172.18.0.1:9335（容器 mcp-relay 发布）
          → host.docker.internal:9334（本脚本）
          → ::1:9333（Playwright MCP）

用法：
  python deploy/mcp-relay.py                     # 默认 0.0.0.0:9334 → ::1:9333
  python deploy/mcp-relay.py --port 9334 --target-port 9333
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError):
        pass
    finally:
        with contextlib.suppress(Exception):
            writer.close()


async def handle(client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter, target: tuple[str, int]):
    try:
        up_reader, up_writer = await asyncio.open_connection(*target)
    except Exception as exc:  # noqa: BLE001
        print(f"[relay] 上游连接失败 {target}: {exc}", flush=True)
        client_writer.close()
        return
    await asyncio.gather(
        _pipe(client_reader, up_writer),
        _pipe(up_reader, client_writer),
    )
    with contextlib.suppress(Exception):
        client_writer.close()
    with contextlib.suppress(Exception):
        up_writer.close()


async def main_async(host: str, port: int, target_host: str, target_port: int) -> None:
    target = (target_host, target_port)
    server = await asyncio.start_server(
        lambda r, w: handle(r, w, target), host=host, port=port
    )
    print(f"[relay] 监听 {host}:{port} → {target_host}:{target_port}", flush=True)
    async with server:
        await server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description="Playwright MCP 宿主中继")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=9334)
    parser.add_argument("--target-host", default="::1", help="MCP 监听地址（默认 IPv6 回环）")
    parser.add_argument("--target-port", type=int, default=9333)
    args = parser.parse_args()
    try:
        asyncio.run(main_async(args.host, args.port, args.target_host, args.target_port))
    except KeyboardInterrupt:
        print("[relay] 已停止", flush=True)


if __name__ == "__main__":
    main()

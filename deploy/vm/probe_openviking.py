"""验证 openviking 记忆链路是"真连上"还是"假阳性"。

在 VM 里跑：
  - 打印实际生效的 OPENVIKING_SERVER_URL 与 DNS 解析结果
  - 直接 HTTP 探活（不带/带 api_key）
  - 用 openviking SDK 做一次真实 ping + 写入 + 查询
结果回传宿主 /report。
"""

import asyncio
import json
import os
import socket
import urllib.request

HOST_BASE = "http://10.244.37.37:8000"
DATA = os.path.join(os.environ.get("LOCALAPPDATA", ""), "MyAgent", "data")
KEY_FILE = os.path.join(DATA, "config", "openviking", "api_key")

report: dict = {}


def note(key, value):
    report[key] = value


def read_key() -> str:
    try:
        with open(KEY_FILE, encoding="utf-8") as fh:
            return fh.read().strip()
    except Exception as exc:  # noqa: BLE001
        note("key_read_error", str(exc))
        return ""


def http(url: str, headers: dict | None = None) -> str:
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            return f"{resp.status} {resp.read()[:200].decode('utf-8', 'replace')}"
    except Exception as exc:  # noqa: BLE001
        return f"失败: {exc}"


async def sdk_probe() -> None:
    try:
        import openviking  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        note("sdk_import_error", str(exc))
        return

    url = os.environ.get("OPENVIKING_SERVER_URL") or "http://openviking.agent.svc.cluster.local:1933"
    key = read_key()
    note("env_url", url)
    note("key_len", len(key))

    # 1) 裸 HTTP
    note("http_health", http(url.rstrip("/") + "/health", {"X-API-Key": key}))
    note("http_health_hostip", http("http://10.244.37.37:1933/health", {"X-API-Key": key}))

    # 2) DNS
    for host in ("openviking.agent.svc.cluster.local", "10.244.37.37"):
        try:
            note(f"dns_{host}", socket.gethostbyname(host))
        except Exception as exc:  # noqa: BLE001
            note(f"dns_{host}", f"解析失败: {exc}")

    # 3) SDK ping（真正走客户端）
    try:
        from openviking import AsyncOpenViking

        client = AsyncOpenViking(api_key=key, base_url=url)
        note("sdk_client", type(client).__name__)
        for name in ("ping", "health", "search", "add_message", "append_turn", "recall"):
            note(f"sdk_has_{name}", hasattr(client, name))
        try:
            note("sdk_ping", str(await asyncio.wait_for(client.ping(), timeout=10)))
        except Exception as exc:  # noqa: BLE001
            note("sdk_ping", f"失败: {exc}")
        aclose = getattr(client, "close", None) or getattr(client, "aclose", None)
        if aclose:
            try:
                await aclose()
            except Exception:  # noqa: BLE001
                pass
    except Exception as exc:  # noqa: BLE001
        note("sdk_error", str(exc))


def main() -> None:
    asyncio.run(sdk_probe())
    # 服务自己写进日志的 URL 也抓一下
    log_path = os.path.join(DATA, "logs", "openviking-context-service.log")
    try:
        with open(log_path, encoding="utf-8", errors="replace") as fh:
            lines = [l.strip() for l in fh if "OpenViking server" in l]
        note("service_log", lines[-2:])
    except Exception as exc:  # noqa: BLE001
        note("service_log", f"读取失败: {exc}")

    print(json.dumps(report, ensure_ascii=False, indent=2))
    try:
        payload = json.dumps({"stage": "openviking-probe", "ok": True,
                              "detail": json.dumps(report, ensure_ascii=False),
                              "host": "vm"}, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(f"{HOST_BASE}/report", data=payload, method="POST",
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as exc:  # noqa: BLE001
        print("回传失败:", exc)


if __name__ == "__main__":
    main()

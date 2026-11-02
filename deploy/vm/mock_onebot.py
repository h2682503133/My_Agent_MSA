"""最小 OneBot 11 模拟服务端：用来在没有真 QQ 的情况下验证适配器。

做三件事：
  1. 起一个 WS 服务端（默认 ws://127.0.0.1:3001/），等适配器连上来；
  2. 连上后推一条私聊消息事件（模拟用户发「在吗」）；
  3. 收到适配器发来的 send_private_msg / send_msg 动作时，把内容打印出来并回 status=ok。

用法（虚拟机里，用共享 venv）：
    python mock_onebot.py [--port 3001] [--user 10001] [--text 在吗] [--wait 90]
"""

import argparse
import asyncio
import json
import time
import urllib.request

HOST_BASE = "http://10.244.37.37:8000"


async def serve(args) -> dict:
    import websockets

    result = {"connected": False, "sent_actions": [], "events_pushed": 0, "error": ""}

    async def handler(ws):
        result["connected"] = True
        print("[mock] 适配器已连接", flush=True)

        # 推一条私聊消息事件（OneBot 11 标准结构）
        event = {
            "time": int(time.time()),
            "self_id": args.self_id,
            "post_type": "message",
            "message_type": "private",
            "sub_type": "friend",
            "message_id": 1001,
            "user_id": args.user,
            "message": [{"type": "text", "data": {"text": args.text}}],
            "raw_message": args.text,
            "font": 0,
            "sender": {"user_id": args.user, "nickname": "mock-user"},
        }
        await ws.send(json.dumps(event, ensure_ascii=False))
        result["events_pushed"] += 1
        print(f"[mock] 已推送私聊消息：{args.text}", flush=True)

        try:
            async for raw in ws:
                try:
                    payload = json.loads(raw)
                except Exception:  # noqa: BLE001
                    print(f"[mock] 收到非 JSON：{raw[:120]}", flush=True)
                    continue
                action = payload.get("action", "")
                params = payload.get("params") or {}
                echo = payload.get("echo", "")
                print(f"[mock] 收到动作 {action}：{json.dumps(params, ensure_ascii=False)[:300]}", flush=True)
                result["sent_actions"].append({"action": action, "params": params})

                # OneBot 11 动作响应：send_private_msg / send_msg / send_group_msg
                resp = {"status": "ok", "retcode": 0, "data": {"message_id": 2000}}
                if echo != "":
                    resp["echo"] = echo
                await ws.send(json.dumps(resp, ensure_ascii=False))
        except Exception as exc:  # noqa: BLE001
            print(f"[mock] 连接结束：{exc}", flush=True)

    async with websockets.serve(handler, args.host, args.port, max_size=None):
        print(f"[mock] 监听 ws://{args.host}:{args.port}/（等适配器连接，最多 {args.wait}s）", flush=True)
        await asyncio.sleep(args.wait)
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=3001)
    parser.add_argument("--user", type=int, default=10001)
    parser.add_argument("--self-id", type=int, default=20002)
    parser.add_argument("--text", default="在吗")
    parser.add_argument("--wait", type=float, default=90)
    args = parser.parse_args()

    result = asyncio.run(serve(args))
    print("=== 结果 ===", flush=True)
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)

    try:
        payload = json.dumps({"stage": "onebot-mock", "ok": bool(result["sent_actions"]),
                              "detail": json.dumps(result, ensure_ascii=False),
                              "host": "vm"}, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(f"{HOST_BASE}/report", data=payload, method="POST",
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10).read()
        print("已回传宿主", flush=True)
    except Exception as exc:  # noqa: BLE001
        print("回传失败:", exc, flush=True)


if __name__ == "__main__":
    main()

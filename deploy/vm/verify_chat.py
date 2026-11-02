"""单机端到端：开 SSE → 发一条消息 → 等 assistant_message 回复。

在虚拟机里跑（只用标准库），结果回传宿主 /report。
"""

import json
import threading
import time
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:5210"
USER_ID = "nh"
CONTENT = "请调用 dir-list 工具看一下工作区根目录，把工具返回的原文贴给我"
HOST_BASE = "http://10.244.37.37:8000"
TIMEOUT = 240

events: list[dict] = []
stop = threading.Event()


def sse_listen() -> None:
    query = urllib.parse.urlencode({"user_id": USER_ID, "agent_id": "main"})
    url = f"{BASE}/api/events?{query}"
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT) as resp:
            buf = ""
            for raw in resp:
                if stop.is_set():
                    break
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                if line.startswith("data:"):
                    payload = line[5:].strip()
                    try:
                        data = json.loads(payload)
                    except Exception:
                        continue
                    events.append(data)
                    if data.get("type") in {"assistant_message", "task_failed", "task_error"}:
                        stop.set()
                        break
    except Exception as exc:  # noqa: BLE001
        events.append({"type": "_sse_error", "error": str(exc)})


def post_message() -> dict:
    body = json.dumps({
        "user_id": USER_ID,
        "content": CONTENT,
        "agent_id": "main",
        "session_id": f"web_{USER_ID}",
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE}/api/messages", data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return {"status": resp.status, "body": resp.read().decode("utf-8", "replace")[:400]}
    except Exception as exc:  # noqa: BLE001
        return {"status": 0, "error": str(exc)}


def main() -> None:
    listener = threading.Thread(target=sse_listen, daemon=True)
    listener.start()
    time.sleep(1.5)

    posted = post_message()
    deadline = time.time() + TIMEOUT
    while time.time() < deadline and not stop.is_set():
        time.sleep(1)

    reply = next((e for e in events if e.get("type") == "assistant_message"), None)
    result = {
        "stage": "chat-sse",
        "posted": posted,
        "event_count": len(events),
        "event_types": [e.get("type") for e in events][:20],
        "reply_text": (reply or {}).get("text", "")[:300],
        "reply_images": (reply or {}).get("images", []),
        "ok": bool(reply),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    try:
        payload = json.dumps({"stage": "chat-sse", "ok": result["ok"],
                              "detail": json.dumps(result, ensure_ascii=False),
                              "host": "vm"}, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(f"{HOST_BASE}/report", data=payload, method="POST",
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as exc:  # noqa: BLE001
        print("回传失败:", exc)


if __name__ == "__main__":
    main()

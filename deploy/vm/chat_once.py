"""从宿主向 VM 单体 gateway 发一条消息并收集回复（自主测试用）。"""
import json
import sys
import threading
import time
import urllib.parse
import urllib.request

BASE = "http://10.244.62.117:5210"
USER_ID = "nh"
TIMEOUT = 240

events = []
stop = threading.Event()


def sse_listen() -> None:
    query = urllib.parse.urlencode({"user_id": USER_ID, "agent_id": "main"})
    url = f"{BASE}/api/events?{query}"
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT) as resp:
            for raw in resp:
                if stop.is_set():
                    break
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                if not line.startswith("data:"):
                    continue
                try:
                    data = json.loads(line[5:].strip())
                except Exception:
                    continue
                events.append(data)
                if data.get("type") in {"assistant_message", "task_failed", "task_error"}:
                    stop.set()
                    break
    except Exception as exc:  # noqa: BLE001
        events.append({"type": "_sse_error", "error": str(exc)})


def main() -> None:
    content = sys.argv[1] if len(sys.argv) > 1 else "在吗"
    images = []
    if len(sys.argv) > 2:
        images = json.load(open(sys.argv[2], encoding="utf-8"))

    t = threading.Thread(target=sse_listen, daemon=True)
    t.start()
    time.sleep(1.5)

    body = json.dumps({
        "user_id": USER_ID,
        "content": content,
        "agent_id": "main",
        "session_id": f"web_{USER_ID}",
        "images": images,
    }).encode("utf-8")
    req = urllib.request.Request(f"{BASE}/api/messages", data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            print("POST:", resp.read().decode()[:200])
    except Exception as exc:  # noqa: BLE001
        print("POST 失败:", exc)
        return

    deadline = time.time() + TIMEOUT
    while time.time() < deadline and not stop.is_set():
        time.sleep(1)

    print("事件数:", len(events), "| 类型:", [e.get("type") for e in events][:10])
    reply = next((e for e in events if e.get("type") == "assistant_message"), None)
    if reply:
        print("回复:", str(reply.get("text") or "")[:900])
        print("图片:", reply.get("images"))
    else:
        print("没有 assistant_message")


if __name__ == "__main__":
    main()

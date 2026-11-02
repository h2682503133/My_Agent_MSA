"""openviking 记忆闭环验证：第一轮写入，第二轮跨任务召回。

在 VM 里跑，结果回传宿主 /report。
"""

import json
import threading
import time
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:5210"
HOST_BASE = "http://10.244.37.37:8000"
USER_ID = "nh"

ROUNDS = [
    ("记住一件事：我养的猫叫雪球，是只白猫。只回一句 ok 就行。", "雪球"),
    ("我养的猫叫什么名字？只回答名字。", "雪球"),
]


def ask(content: str, timeout: float = 180) -> dict:
    events: list[dict] = []
    done = threading.Event()

    def listen() -> None:
        query = urllib.parse.urlencode({"user_id": USER_ID, "agent_id": "main"})
        try:
            with urllib.request.urlopen(f"{BASE}/api/events?{query}", timeout=timeout) as resp:
                for raw in resp:
                    if done.is_set():
                        break
                    line = raw.decode("utf-8", "replace").rstrip("\r\n")
                    if not line.startswith("data:"):
                        continue
                    try:
                        data = json.loads(line[5:].strip())
                    except Exception:  # noqa: BLE001
                        continue
                    events.append(data)
                    if data.get("type") == "assistant_message":
                        done.set()
                        break
        except Exception as exc:  # noqa: BLE001
            events.append({"type": "_sse_error", "error": str(exc)})

    thread = threading.Thread(target=listen, daemon=True)
    thread.start()
    time.sleep(1.5)

    body = json.dumps({
        "user_id": USER_ID, "content": content, "agent_id": "main",
        "session_id": f"web_{USER_ID}",
    }).encode("utf-8")
    req = urllib.request.Request(f"{BASE}/api/messages", data=body, method="POST",
                                headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            posted = {"status": resp.status, "body": resp.read().decode("utf-8", "replace")[:200]}
    except Exception as exc:  # noqa: BLE001
        posted = {"status": 0, "error": str(exc)}

    deadline = time.time() + timeout
    while time.time() < deadline and not done.is_set():
        time.sleep(1)

    reply = next((e for e in events if e.get("type") == "assistant_message"), {})
    return {
        "posted": posted,
        "reply": (reply.get("text") or "")[:400],
        "event_types": [e.get("type") for e in events][:12],
    }


def main() -> None:
    results = []
    for index, (content, expect) in enumerate(ROUNDS, 1):
        outcome = ask(content)
        outcome["expect"] = expect
        outcome["hit"] = expect in (outcome.get("reply") or "")
        results.append(outcome)
        print(json.dumps({"round": index, **outcome}, ensure_ascii=False, indent=2), flush=True)
        time.sleep(3)

    ok = bool(results[-1].get("hit"))
    payload = json.dumps({"stage": "openviking-memory", "ok": ok,
                          "detail": json.dumps(results, ensure_ascii=False),
                          "host": "vm"}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(f"{HOST_BASE}/report", data=payload, method="POST",
                                headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10).read()
        print("已回传宿主")
    except Exception as exc:  # noqa: BLE001
        print("回传失败:", exc)


if __name__ == "__main__":
    main()

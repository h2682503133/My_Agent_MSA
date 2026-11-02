import urllib.request
base = "http://frontend-service.agent.svc.cluster.local"
paths = ["/api/assets/emoji_happy_512-6a063844.png", "/api/assets/reply-5ff9f92c7f76.jpg"]
for path in paths:
    try:
        with urllib.request.urlopen(base + path, timeout=10) as resp:
            body = resp.read()
            print("  ", path, "->", resp.status, len(body), "bytes", resp.headers.get("Content-Type"))
    except Exception as exc:
        print("  ", path, "-> FAILED:", exc)

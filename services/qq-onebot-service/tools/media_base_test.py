"""图片地址补全测试（不联网）。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import config  # noqa: E402
from app.onebot_bridge import OneBotBridge  # noqa: E402

FAILS = []
CHECK = 0

def check(name, got, want):
    global CHECK
    CHECK += 1
    if got == want:
        print(f"  ok    {name}")
    else:
        FAILS.append(name)
        print(f"  FAIL  {name}: got={got!r} want={want!r}")

f = OneBotBridge._absolute_media

config.ONEBOT_MEDIA_BASE = ""
check("无基址：相对路径原样", f("/api/assets/a.jpg"), "/api/assets/a.jpg")
check("无基址：绝对地址原样", f("http://x/a.jpg"), "http://x/a.jpg")

config.ONEBOT_MEDIA_BASE = "http://10.244.62.117:5210"
check("补全相对路径", f("/api/assets/a.jpg"), "http://10.244.62.117:5210/api/assets/a.jpg")
check("补全无斜杠相对路径", f("api/assets/a.jpg"), "http://10.244.62.117:5210/api/assets/a.jpg")
check("绝对地址不动", f("http://10.244.62.117:5102/assets/b.png"), "http://10.244.62.117:5102/assets/b.png")
check("base64 不动", f("base64://AAAA"), "base64://AAAA")
check("data: 不动", f("data:image/png;base64,AA"), "data:image/png;base64,AA")
check("file:// 不动", f("file:///C:/a.png"), "file:///C:/a.png")
check("空串不动", f(""), "")

print()
if FAILS:
    print(f"FAILED {len(FAILS)}: {FAILS}")
    raise SystemExit(1)
print(f"ALL OK — {CHECK} checks passed")

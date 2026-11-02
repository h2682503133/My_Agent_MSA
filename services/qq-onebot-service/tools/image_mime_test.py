"""图片 mime 推断 + data URL 转换（纯函数，不联网）。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.main import _mime_of  # noqa: E402

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

check("PNG 文件头", _mime_of(b"\x89PNG\r\n\x1a\n" + b"0" * 8, "http://x/a"), "image/png")
check("JPEG 文件头", _mime_of(b"\xff\xd8\xff\xe0" + b"0" * 12, "http://x/a"), "image/jpeg")
check("GIF 文件头", _mime_of(b"GIF89a" + b"0" * 10, "http://x/a"), "image/gif")
check("WEBP 文件头", _mime_of(b"RIFF" + b"\x00" * 4 + b"WEBP" + b"0" * 4, "http://x/a"), "image/webp")
check("无可识别头 → 按扩展名", _mime_of(b"\x00" * 16, "http://x/a.png?x=1"), "image/png")
check("都认不出 → 兜底 jpeg", _mime_of(b"\x00" * 16, "http://x/blob"), "image/jpeg")

print()
if FAILS:
    print(f"FAILED {len(FAILS)}: {FAILS}")
    raise SystemExit(1)
print(f"ALL OK — {CHECK} checks passed")

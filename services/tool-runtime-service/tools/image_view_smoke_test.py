"""image-view 工具（app/image_tools.py）冒烟测试。

覆盖：
1. 真实文件头识别（png / jpeg / gif / webp / bmp）与尺寸解析
2. 工作区相对路径 → data URL（mime 以真实字节为准，不信后缀）
3. 失败必须显式报错：非图片 / 越界路径 / 不存在 / 超体积
4. 空参数报错（不能静默返回空图）

运行：python tools/image_view_smoke_test.py
"""

import struct
import sys
import tempfile
from pathlib import Path

_here = Path(__file__).resolve()
APP_DIR = _here.parent.parent
for _candidate in (_here.parent.parent, _here.parent, Path("/app"), Path.cwd()):
    if (_candidate / "app" / "image_tools.py").exists():
        APP_DIR = _candidate
        break
sys.path.insert(0, str(APP_DIR))

from app.image_tools import (  # noqa: E402
    format_bytes,
    image_dimensions,
    load_image,
    sniff_magic,
)

FAILURES = []
CHECKS = 0


def check(name, condition):
    global CHECKS
    CHECKS += 1
    if not condition:
        FAILURES.append(name)
        print(f"  FAIL  {name}")
    else:
        print(f"  ok    {name}")


def png(width, height):
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return sig + struct.pack(">I", len(ihdr)) + b"IHDR" + ihdr + b"\x00\x00\x00\x00"


def jpeg(width, height):
    # SOI + SOF0（段长 11，精度 8，高 2 字节，宽 2 字节）+ 占位数据
    sof = b"\xff\xc0" + struct.pack(">H", 11) + b"\x08" + struct.pack(">HH", height, width) + b"\x03"
    return b"\xff\xd8" + sof + b"\x00" * 32


def gif(width, height):
    return b"GIF89a" + struct.pack("<HH", width, height) + b"\x00" * 16


def webp_vp8x(width, height):
    body = (
        b"WEBP"
        + b"VP8X"
        + struct.pack("<I", 10)
        + b"\x00\x00\x00\x00"
        + (width - 1).to_bytes(3, "little")
        + (height - 1).to_bytes(3, "little")
    )
    return b"RIFF" + struct.pack("<I", len(body)) + body


def bmp(width, height):
    header = bytearray(54)
    header[0:2] = b"BM"
    header[18:22] = struct.pack("<i", width)
    header[22:26] = struct.pack("<i", height)
    return bytes(header)


print("=== 文件头识别 ===")
check("png", sniff_magic(png(4, 3)) == ("image/png", ".png"))
check("jpeg", sniff_magic(jpeg(4, 3)) == ("image/jpeg", ".jpg"))
check("gif", sniff_magic(gif(4, 3)) == ("image/gif", ".gif"))
check("webp", sniff_magic(webp_vp8x(4, 3)) == ("image/webp", ".webp"))
check("bmp", sniff_magic(bmp(4, 3)) == ("image/bmp", ".bmp"))
check("文本不是图片", sniff_magic(b"hello world, not an image") is None)

print("=== 尺寸解析 ===")
check("png 尺寸", image_dimensions(png(640, 480)) == (640, 480))
check("jpeg 尺寸", image_dimensions(jpeg(1280, 720)) == (1280, 720))
check("gif 尺寸", image_dimensions(gif(320, 200)) == (320, 200))
check("webp 尺寸", image_dimensions(webp_vp8x(1920, 1080)) == (1920, 1080))
check("bmp 尺寸", image_dimensions(bmp(100, 50)) == (100, 50))
check("截断的 jpeg 不崩", image_dimensions(b"\xff\xd8\xff\xc0") is None)

print("=== 工作区读取 ===")
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp).resolve()
    (root / "uploads" / "images").mkdir(parents=True)
    # 后缀写错（.txt）也必须按真实字节识别成 png
    (root / "uploads" / "images" / "shot.txt").write_bytes(png(10, 20))

    payload = load_image("uploads/images/shot.txt", root)
    check("relpath 归一", payload.relpath == "uploads/images/shot.txt")
    check("mime 按字节", payload.mime == "image/png")
    check("尺寸", (payload.width, payload.height) == (10, 20))
    check("data URL 前缀", payload.data_url.startswith("data:image/png;base64,"))
    check("size_text 可读", payload.size_text.endswith(("B", "KB", "MB")))

    (root / "notes.txt").write_text("hello", encoding="utf-8")
    try:
        load_image("notes.txt", root)
        check("非图片应报错", False)
    except ValueError as exc:
        check("非图片应报错", "不是支持的图片格式" in str(exc))

    try:
        load_image("../../etc/passwd", root)
        check("越界路径应报错", False)
    except Exception:
        check("越界路径应报错", True)

    try:
        load_image("uploads/images/missing.png", root)
        check("不存在应报错", False)
    except Exception:
        check("不存在应报错", True)

    try:
        load_image("uploads/images/shot.txt", root, max_bytes=8)
        check("超体积应报错", False)
    except ValueError as exc:
        check("超体积应报错", "图片过大" in str(exc))

    try:
        load_image("", root)
        check("空参数应报错", False)
    except ValueError:
        check("空参数应报错", True)

print("=== 其它 ===")
check("format_bytes B", format_bytes(512) == "512 B")
check("format_bytes KB", format_bytes(2048) == "2.0 KB")
check("format_bytes MB", format_bytes(3 * 1024 * 1024) == "3.00 MB")

print()
if FAILURES:
    print(f"FAILED {len(FAILURES)}/{CHECKS}: {FAILURES}")
    raise SystemExit(1)
print(f"OK {CHECKS} checks")

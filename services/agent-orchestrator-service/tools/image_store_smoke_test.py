"""图片自动落盘（app/image_store.py）冒烟测试。

覆盖：
1. data URL 解析：真实文件头优先于渠道给的 mime；非法输入返回 None 而不是抛错
2. 落盘路径：`uploads/images/<YYYYMMDD>/<sha1 前16>.<ext>`（以用户工作区为基准）
3. 去重：同一张图重复发送 → 同一个文件、同一个引用
4. 批量落盘：坏图跳过、好图照存（失败闭合）
5. 提示词/历史用的图片标记：空引用为空串，多条用「、」连接
6. user_id 目录名按 tool-runtime 同一规则消毒（两边 workspace_dir 必须一致）

运行：python tools/image_store_smoke_test.py
"""

import base64
import re
import struct
import sys
import tempfile
from pathlib import Path

_here = Path(__file__).resolve()
APP_DIR = _here.parent.parent
for _candidate in (_here.parent.parent, _here.parent, Path("/app"), Path.cwd()):
    if (_candidate / "app" / "image_store.py").exists():
        APP_DIR = _candidate
        break
sys.path.insert(0, str(APP_DIR))

from app.image_store import (  # noqa: E402
    IMAGE_SUBDIR,
    image_marker,
    parse_data_url,
    safe_user_segment,
    sniff_ext,
    store_image,
    store_images,
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


def png(width=8, height=6):
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return sig + struct.pack(">I", len(ihdr)) + b"IHDR" + ihdr + b"\x00\x00\x00\x00"


def jpeg():
    return b"\xff\xd8\xff\xe0" + b"\x00" * 40


def data_url(data, mime="image/jpeg"):
    return f"data:{mime};base64," + base64.b64encode(data).decode("ascii")


print("=== parse_data_url ===")
parsed = parse_data_url(data_url(png(), mime="image/jpeg"))
check("真实字节优先于 mime（jpeg 标签 + png 内容 → .png）", parsed == (png(), ".png"))
check("jpeg 内容 → .jpg", parse_data_url(data_url(jpeg()))[1] == ".jpg")
check("非 data URL → None", parse_data_url("http://example.com/a.png") is None)
check("非 base64 data URL → None", parse_data_url("data:image/png,abc") is None)
check("空载荷 → None", parse_data_url("data:image/png;base64,") is None)
check(
    "未知类型 → None",
    parse_data_url("data:application/pdf;base64," + base64.b64encode(b"PK").decode()) is None,
)
check("sniff_ext 文本 → 空", sniff_ext(b"just text") == "")

print("=== 落盘 ===")
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp).resolve()
    ref = store_image(root, "h268", data_url(png()))
    check("引用以 uploads/images 开头", ref.startswith(IMAGE_SUBDIR + "/"))
    check("引用按日期分目录", re.fullmatch(r"uploads/images/\d{8}/[0-9a-f]{16}\.png", ref) is not None)

    stored = root / "users" / "h268" / ref
    check("文件真的写到了用户工作区", stored.is_file())
    check("写入字节与源一致", stored.read_bytes() == png())

    again = store_image(root, "h268", data_url(png()))
    check("同一张图 → 同一个引用（去重）", again == ref)
    files = sorted((root / "users" / "h268" / IMAGE_SUBDIR).glob("*/*"))
    check("同一张图只有一个文件", len(files) == 1)

    other = store_image(root, "h268", data_url(jpeg()))
    check("不同图片 → 不同引用", other != ref)

    check("坏图返回 None（不抛错）", store_image(root, "h268", "data:image/png;base64,@@@") is None)
    check("非 data URL 返回 None", store_image(root, "h268", "https://x/y.png") is None)

    many = store_images(root, "h268", [data_url(jpeg()), "not-a-data-url", data_url(png())])
    check("批量落盘跳过坏图", len(many) == 2)
    check("批量结果含已存在的引用", ref in many)

print("=== 用户目录消毒 ===")
check("斜杠/点点被替换", safe_user_segment("a/../b") == "a_.._b")
check("空 → default", safe_user_segment("") == "default")
check("正常 id 不变", safe_user_segment("qq_2195239690") == "qq_2195239690")

with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp).resolve()
    store_image(root, "qq_2195239690", data_url(png()))
    check("落盘落在 users/<user_id>/ 下", (root / "users" / "qq_2195239690" / IMAGE_SUBDIR).is_dir())

print("=== image_marker ===")
check("空引用 → 空串", image_marker([]) == "")
check("None → 空串", image_marker(None) == "")
marker = image_marker(["uploads/images/20260101/a.png", "uploads/images/20260101/b.jpg"])
check("标记带路径", "uploads/images/20260101/a.png" in marker and "b.jpg" in marker)
check("标记用「、」连接", "、" in marker)
check("标记只有一个换行前缀", marker.startswith("\n（附图: ") and marker.count("\n") == 1)

print()
if FAILURES:
    print(f"FAILED {len(FAILURES)}/{CHECKS}: {FAILURES}")
    raise SystemExit(1)
print(f"OK {CHECKS} checks")

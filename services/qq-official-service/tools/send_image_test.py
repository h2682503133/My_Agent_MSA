"""官方渠道发图自检（不联网：桩掉 openapi 与下载）。

覆盖：
 1. 纯文本：走 send_message
 2. 纯图片：跳过文本，上传 + msg_type=7
 3. 文本 + 图片：先文本后图片，两者都 True 才算成功
 4. 上传失败 → False 且不改发文本（已发过文本时仍返回 False 供调度器切换）
 5. data URL / 本地文件 两种取图来源
"""

import asyncio
import base64
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import types

# 本机没有 aiohttp（服务镜像里才有），自检只需要它的几个符号
aiohttp_stub = types.ModuleType("aiohttp")
aiohttp_stub.ClientSession = type("ClientSession", (), {})
aiohttp_stub.ClientTimeout = type("ClientTimeout", (), {"__init__": lambda self, **kw: None})


class _Resp:
    status = 200

    async def read(self):
        return b""


sys.modules["aiohttp"] = aiohttp_stub

from app.bridge import OfficialBridge  # noqa: E402

FAILS = []
CHECKS = 0


def check(name, cond, extra=""):
    global CHECKS
    CHECKS += 1
    if cond:
        print(f"  ok    {name}")
    else:
        FAILS.append(name)
        print(f"  FAIL  {name} {extra}")


class FakeOpenApi:
    def __init__(self, upload_ok=True, send_media_ok=True, text_ok=True):
        self.upload_ok = upload_ok
        self.send_media_ok = send_media_ok
        self.text_ok = text_ok
        self.texts = []
        self.uploads = []
        self.medias = []

    async def send_message(self, kind, target, content, msg_id="", msg_seq=None):
        self.texts.append(content)
        return {"ok": self.text_ok, "err_code": 0 if self.text_ok else 40034, "message": ""}

    async def upload_media(self, kind, target, file_data, file_type=1):
        self.uploads.append((file_type, len(file_data)))
        if not self.upload_ok:
            return {"ok": False, "err_code": 40005, "message": "no permission", "http_status": 403}
        return {"ok": True, "file_info": "FILEINFO", "file_uuid": "u", "err_code": 0, "message": ""}

    async def send_media_message(self, kind, target, file_info, msg_id="", msg_seq=None):
        self.medias.append(file_info)
        if not self.send_media_ok:
            return {"ok": False, "err_code": 40034, "message": "bad", "http_status": 400}
        return {"ok": True, "err_code": 0, "message": "", "id": "MID"}


def build_bridge(openapi):
    bridge = OfficialBridge.__new__(OfficialBridge)
    bridge.openapi = openapi
    bridge._seq = {}
    bridge._passive = {}
    bridge._record_passive("private", "ADDR", "MSGID")
    return bridge


async def main():
    png = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 32).decode()
    data_url = f"data:image/png;base64,{png}"

    print("纯文本")
    api = FakeOpenApi()
    bridge = build_bridge(api)
    ok = await bridge._send_with_target("private", "ADDR", "hello", [], reserve=0)
    check("返回 True", ok is True, str(ok))
    check("只发文本", len(api.texts) == 1 and not api.uploads, f"{api.texts} {api.uploads}")

    print("纯图片")
    api = FakeOpenApi()
    bridge = build_bridge(api)
    ok = await bridge._send_with_target("private", "ADDR", "", [data_url], reserve=0)
    check("返回 True", ok is True, str(ok))
    check("没发文本", api.texts == [], str(api.texts))
    check("上传了一次", api.uploads == [(1, len(base64.b64decode(png)))], str(api.uploads))
    check("发了媒体消息", api.medias == ["FILEINFO"], str(api.medias))

    print("文本 + 图片")
    api = FakeOpenApi()
    bridge = build_bridge(api)
    ok = await bridge._send_with_target("private", "ADDR", "看图", [data_url, data_url], reserve=0)
    check("返回 True", ok is True)
    check("先文本后两张图", len(api.texts) == 1 and len(api.uploads) == 2 and len(api.medias) == 2,
          f"{len(api.texts)}/{len(api.uploads)}/{len(api.medias)}")

    print("上传失败 → 文本照发 + 失败原因告诉用户")
    api = FakeOpenApi(upload_ok=False)
    bridge = build_bridge(api)
    ok = await bridge._send_with_target("private", "ADDR", "看图", [data_url], reserve=0)
    check("仍算投递成功（提示已发出）", ok is True, str(ok))
    check("文本发出", len(api.texts) == 2, str(api.texts))
    check("第二条是失败提示", "图片发送失败" in api.texts[-1], api.texts[-1][:60])
    check("提示里带错误码", "40005" in api.texts[-1], api.texts[-1][:80])
    check("提示里带图片链接", "data:image/png" in api.texts[-1], api.texts[-1][-60:])
    check("没有发媒体", api.medias == [], str(api.medias))

    print("媒体发送失败 → 纯图片轮也改成发提示")
    api = FakeOpenApi(send_media_ok=False)
    bridge = build_bridge(api)
    ok = await bridge._send_with_target("private", "ADDR", "", [data_url], reserve=0)
    check("算投递成功", ok is True, str(ok))
    check("发了一条失败提示", len(api.texts) == 1 and "图片发送失败" in api.texts[0], str(api.texts))

    print("本地文件来源")
    tmp = Path(tempfile.mkdtemp())
    try:
        image = tmp / "a.png"
        image.write_bytes(base64.b64decode(png))
        api = FakeOpenApi()
        bridge = build_bridge(api)
        ok = await bridge._send_with_target("private", "ADDR", "", [str(image)], reserve=0)
        check("本地文件可发", ok is True, str(ok))
        check("字节数正确", api.uploads == [(1, len(base64.b64decode(png)))], str(api.uploads))
    finally:
        for child in tmp.iterdir():
            child.unlink()
        tmp.rmdir()

    print("文件不存在 → 提示用户，不抛异常")
    api = FakeOpenApi()
    bridge = build_bridge(api)
    ok = await bridge._send_with_target("private", "ADDR", "", ["/nope/missing.png"], reserve=0)
    check("算投递成功", ok is True, str(ok))
    check("提示里有取图失败原因", "取图失败" in api.texts[0], api.texts[0][:60])

    print()
    if FAILS:
        print(f"FAILED {len(FAILS)}: {FAILS}")
        return 1
    print(f"ALL OK — {CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

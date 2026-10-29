"""qq-llbot 入站解析自检：图文消息的 <img>、转发标记、表情/引用等。

不依赖 satori 运行时：用轻量假元素模拟 Element(type=…)。
"""
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class FakeElem:
    def __init__(self, kind, body="", **attrs):
        self.type = kind
        self._body = body
        self.attrs = attrs
        if kind in ("", "text"):
            self.type = "text"

    def __str__(self):
        return self._body or ""


# satori 的 Text/Image 需要打桩，否则 import 时缺失
satori_mod = types.ModuleType("satori")
satori_mod.client = types.ModuleType("satori.client")
satori_mod.client.App = type("App", (), {})
satori_mod.client.WebsocketsInfo = type("WebsocketsInfo", (), {})
satori_mod.client.Account = type("Account", (), {})
satori_mod.const = types.ModuleType("satori.const")
satori_mod.const.EventType = type("EventType", (), {"MESSAGE_CREATED": "message-created"})
satori_mod.event = types.ModuleType("satori.event")
satori_mod.event.MessageEvent = type("MessageEvent", (), {})
satori_mod.model = types.ModuleType("satori.model")
satori_mod.model.ChannelType = type("ChannelType", (), {})


class Text:
    def __init__(self, text):
        self._text = text

    def __str__(self):
        return self._text


class Image:
    def __init__(self, src):
        self.src = src


class File:
    def __init__(self, src, title=""):
        self.src = src
        self.title = title


satori_mod.element = types.ModuleType("satori.element")
satori_mod.element.At = type("At", (), {})
satori_mod.element.Text = Text
satori_mod.element.Image = Image
satori_mod.element.File = File
satori_mod.element.Audio = type("Audio", (), {})
satori_mod.element.Video = type("Video", (), {})

aiohttp_stub = types.ModuleType("aiohttp")
aiohttp_stub.ClientSession = type("ClientSession", (), {})
aiohttp_stub.ClientTimeout = type("ClientTimeout", (), {"__init__": lambda self, **kw: None})
sys.modules["aiohttp"] = aiohttp_stub

sys.modules["satori"] = satori_mod
sys.modules["satori.client"] = satori_mod.client
sys.modules["satori.const"] = satori_mod.const
sys.modules["satori.event"] = satori_mod.event
sys.modules["satori.model"] = satori_mod.model
sys.modules["satori.element"] = satori_mod.element

from app import qq_bridge as bridge  # noqa: E402

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


def main():
    print("图文消息：<img> 在文本里")
    html = '看看这位<img src="https://multimedia.nt.qq.com.cn/download?x=1" />'
    elems = [Text(html)]
    text = bridge._strip_at(elems)
    images = bridge._extract_images(elems)
    check("正文不再含 HTML 标签", "<img" not in text, repr(text))
    check("正文保留文字", "看看这位" in text, repr(text))
    check("图片被抽进 images", images == ["https://multimedia.nt.qq.com.cn/download?x=1"], str(images))

    print("标准 Image 元素")
    elems2 = [Text("这张："), Image("https://example.com/a.png")]
    check("Image 正常抽取", bridge._extract_images(elems2) == ["https://example.com/a.png"])
    check("Image 不进正文", "example.com" not in bridge._strip_at(elems2))

    print("去重")
    elems3 = [Text('<img src="https://a/x.png"><img src="https://a/x.png">')]
    check("同一 URL 只留一份", bridge._extract_images(elems3) == ["https://a/x.png"], str(bridge._extract_images(elems3)))

    print("转发 / 卡片")
    check("forward 有标记", bridge._render_element(FakeElem("forward")) == "[转发内容]")
    check("json 卡片有标记", bridge._render_element(FakeElem("json")) == "[转发内容]")

    print("既有行为不回归")
    check("quote 仍丢弃", bridge._render_element(FakeElem("quote")) == "")
    check("未知类型仍保留占位", bridge._render_element(FakeElem("video2")) == "[video2]")

    print()
    if FAILS:
        print(f"FAILED {len(FAILS)}: {FAILS}")
        return 1
    print(f"ALL OK — {CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

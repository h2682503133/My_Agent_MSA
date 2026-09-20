"""QQ 推送路径冒烟测试（P3：从 session_id 重建推送目标）。

需要 satori（容器内运行）；本地缺依赖时会明确 SKIP。

核心断言：
- `_sessions` 未命中时不再直接放弃，而是从 session_id 重建目标并真的发出去
- 私聊走 user_channel_create → send_message（**不能**用 send_private_message，
  它会把元素列表拍平成字符串、丢掉图片/文件语义）
- 群聊直接 send_message(群号, ...)
- 缓存命中时仍走 event 路径（保留 referrer / 引用回复样式）
- 无法解析的会话格式 → 返回 False（交给上层回报投递失败）

注意：`satori.client.App` 是**进程内单例**（重复构造会抛
"App instance already exists"），所以全程只构造一个 QQBridge。

运行：python tools/qq_send_path_smoke_test.py
"""

import asyncio
import sys
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVICE_DIR))

try:
    from app.qq_bridge import QQBridge  # noqa: E402
except ModuleNotFoundError as exc:
    print(f"SKIP: 缺少 {exc}（本测试需在 qq-llbot 容器内运行）")
    sys.exit(0)

FAILURES = []
CHECKS = 0


def check(name, condition, extra=""):
    global CHECKS
    CHECKS += 1
    if not condition:
        FAILURES.append(name)
        print(f"  FAIL  {name} {extra}")
    else:
        print(f"  ok    {name}")


class FakeAccount:
    """记录调用序列的假账号，用来验证 send 走了哪条路径。"""

    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    async def user_channel_create(self, user_id=None, guild_id=None):
        self.calls.append(("user_channel_create", user_id))
        return f"channel-for-{user_id}"

    async def send_message(self, channel, message, referrer=None):
        self.calls.append(("send_message", channel, type(message).__name__, len(message)))
        if self.fail:
            raise RuntimeError("Cannot connect to host host.docker.internal:5600")
        return []

    async def send(self, event, message):
        self.calls.append(("send", "event-path"))
        if self.fail:
            raise RuntimeError("Cannot connect to host host.docker.internal:5600")
        return []


BRIDGE = QQBridge()   # 单例：全程复用


def reset(account):
    BRIDGE._sessions.clear()
    BRIDGE._pick_account = lambda: account
    return account


async def main():
    print("=== 1. 重建目标：私聊 ===")
    fake = reset(FakeAccount())
    ok = await BRIDGE.send("qq_2195239690", text="测试推送")
    check("私聊重建路径返回成功", ok is True)
    check("先解析私聊 channel", fake.calls and fake.calls[0][0] == "user_channel_create",
          f"calls={fake.calls}")
    check("解析用的 user_id 是 QQ 号", fake.calls and fake.calls[0][1] == "2195239690")
    check("再向该 channel 发送", len(fake.calls) == 2 and fake.calls[1][0] == "send_message")
    check("发送目标是解析出的 channel", fake.calls[1][1] == "channel-for-2195239690")
    check("**没有走 _sessions**（未命中也不放弃）", not any(c[0] == "send" for c in fake.calls))

    print("\n=== 2. 重建目标：群聊 ===")
    fake = reset(FakeAccount())
    ok = await BRIDGE.send("qq_g_851648664", text="群消息")
    check("群聊重建路径返回成功", ok is True)
    check("群聊直接 send_message(群号)", fake.calls and fake.calls[0][0] == "send_message")
    check("发送目标是群号本身", fake.calls and fake.calls[0][1] == "851648664")
    check("群聊不需要解析 channel", not any(c[0] == "user_channel_create" for c in fake.calls))

    print("\n=== 3. 缓存命中仍走 event 路径（保留引用回复）===")
    fake = reset(FakeAccount())
    BRIDGE._sessions["qq_2195239690"] = (fake, object())   # 伪造本轮入站事件
    ok = await BRIDGE.send("qq_2195239690", text="回复")
    check("缓存命中返回成功", ok is True)
    check("走 event 路径（account.send）", fake.calls and fake.calls[0][0] == "send",
          f"calls={fake.calls}")

    print("\n=== 4. 发送失败 → 返回 False（供上层 ack 失败）===")
    fake = reset(FakeAccount(fail=True))
    ok = await BRIDGE.send("qq_2195239690", text="推送")
    check("重建路径失败返回 False", ok is False)
    check("失败前确实尝试了发送", any(c[0] == "send_message" for c in fake.calls))

    fake = reset(FakeAccount(fail=True))
    BRIDGE._sessions["qq_2195239690"] = (fake, object())
    ok = await BRIDGE.send("qq_2195239690", text="回复")
    check("event 路径失败返回 False", ok is False)

    print("\n=== 5. 无可用账号 ===")
    reset(None)
    ok = await BRIDGE.send("qq_2195239690", text="推送")
    check("Satori 未登录时返回 False", ok is False)

    print("\n=== 6. 无法解析的会话格式 ===")
    fake = reset(FakeAccount())
    for bad in ("web_h268", "grp_agents_chat_xiunv5", "qq_", ""):
        ok = await BRIDGE.send(bad, text="x")
        check(f"无法送达 {bad!r} → False", ok is False)
    check("未对非法会话发起任何发送", not fake.calls, f"calls={fake.calls}")

    print("\n=== 7. 空消息不发送 ===")
    fake = reset(FakeAccount())
    ok = await BRIDGE.send("qq_2195239690")
    check("无内容时返回 False 且不调用 API", ok is False and not fake.calls)

    print("\n=== 8. 图片元素不被拍平（元素列表透传）===")
    fake = reset(FakeAccount())
    await BRIDGE.send("qq_2195239690", text="带图", images=["https://example.com/a.png"])
    send_calls = [c for c in fake.calls if c[0] == "send_message"]
    check("消息以元素列表（不止 1 个元素）发送",
          bool(send_calls) and send_calls[0][2] == "list" and send_calls[0][3] >= 2,
          f"calls={fake.calls}")

    print("\n=== 9. is_qq_online 反映账号连接状态 ===")
    reset(None)
    check("无账号 → 不在线", BRIDGE.is_qq_online() is False)

    class Conn:
        def __init__(self, v):
            self._v = v

        def is_set(self):
            return self._v

    class Acc:
        def __init__(self, v):
            self.connected = Conn(v)

    BRIDGE._pick_account = lambda: Acc(True)
    check("connected 已就绪 → 在线", BRIDGE.is_qq_online() is True)
    BRIDGE._pick_account = lambda: Acc(False)
    check("connected 未就绪 → 不在线", BRIDGE.is_qq_online() is False)


asyncio.get_event_loop().run_until_complete(main())

print()
if FAILURES:
    print(f"FAILURES ({len(FAILURES)}):")
    for name in FAILURES:
        print(f"  - {name}")
    sys.exit(1)
print(f"ALL OK — {CHECKS} checks passed")

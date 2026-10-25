"""bridge 离线自测：不联网、不需要 gRPC/proto 生成。

覆盖：
  1. 私聊入站：openid → 规范 user_id、session id 形状、渠道名、client_message_id
  2. 重复推送去重（msg_id + msg_idx）
  3. 群聊入站 + group_map 映射成 qq_g_{群号}
  4. 出站被动回复：msg_id + 递增 msg_seq，额度用尽退化为主动消息
  5. 长度超限折半重发
  6. msg_id 失效后自动改发主动消息

运行：python tools/bridge_smoke_test.py
"""
import asyncio
import os
import sys
import types

SERVICE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SERVICE_ROOT)

# bridge 里 aiohttp 只用于下载图片，本测试不触发；给个占位模块避免导入失败
if "aiohttp" not in sys.modules:
    sys.modules["aiohttp"] = types.ModuleType("aiohttp")

from app import config  # noqa: E402
from app.bridge import OfficialBridge  # noqa: E402


class FakeScheduler:
    def __init__(self):
        self.tasks = []

    async def create_task(self, **kwargs):
        self.tasks.append(kwargs)
        return {"ok": True, "task_id": f"task-{len(self.tasks)}", "error": ""}


class FakeUsers:
    """forward: openid → 规范 id；channels: 规范 id → {渠道: 渠道身份}。"""

    def __init__(
        self,
        forward: dict[str, str] | None = None,
        channels: dict[str, dict[str, str]] | None = None,
        bind_ok: bool = True,
    ):
        self.forward = forward or {}
        self.channels = channels or {}
        self.bind_ok = bind_ok
        self.binds = []

    async def resolve_canonical(self, openid: str) -> str:
        return self.forward.get(openid, openid)

    async def channel_user_id(self, user_id: str, channel: str = "") -> str:
        return (self.channels.get(user_id) or {}).get(channel, "")

    async def bind(self, user_id: str, channel_user_id: str, priority: int = 0):
        self.binds.append({"user_id": user_id, "channel_user_id": channel_user_id})
        return (True, "ok") if self.bind_ok else (False, "渠道身份已被占用")


class FakeOpenApi:
    """按脚本返回结果；脚本用完后一律成功。"""

    def __init__(self, script=None):
        self.calls = []
        self.script = list(script or [])

    async def send_message(self, kind, target, content, msg_id="", msg_seq=None):
        self.calls.append(
            {"kind": kind, "target": target, "content": content, "msg_id": msg_id, "seq": msg_seq}
        )
        if self.script:
            return self.script.pop(0)
        return {"ok": True, "err_code": 0, "message": "", "id": f"m{len(self.calls)}"}


def event_private(msg_id="m1", idx="REFIDX_1", text=" 你好 ", openid="OPENID_A"):
    return {
        "id": msg_id,
        "author": {"user_openid": openid, "member_openid": ""},
        "content": text,
        "message_type": 0,
        "message_scene": {"source": "default", "ext": [f"msg_idx={idx}"]},
    }


def event_group(msg_id="g1", idx="REFIDX_G", text=" /今日天气 ", group="GROUP_OPENID_X", member="MEMBER_1"):
    return {
        "id": msg_id,
        "author": {"user_openid": "", "member_openid": member},
        "content": text,
        "group_openid": group,
        "message_type": 0,
        "message_scene": {"source": "default", "ext": [f"msg_idx={idx}"]},
    }


async def case_inbound_private():
    sched = FakeScheduler()
    bridge = OfficialBridge(sched, FakeUsers({"OPENID_A": "2195239690"}), FakeOpenApi())
    await bridge.handle_dispatch("C2C_MESSAGE_CREATE", event_private())
    assert len(sched.tasks) == 1, sched.tasks
    task = sched.tasks[0]
    assert task["user_id"] == "2195239690", task
    assert task["session_id"] == "qq_2195239690", task
    assert task["channel"] == config.CHANNEL == "qqofficial", task
    assert task["content"] == "你好", task
    assert task["client_message_id"] == "m1", task
    print("[OK] 私聊入站：openid 解析为规范 id，session=qq_{规范id}")


async def case_dedupe():
    sched = FakeScheduler()
    bridge = OfficialBridge(sched, FakeUsers(), FakeOpenApi())
    await bridge.handle_dispatch("C2C_MESSAGE_CREATE", event_private())
    await bridge.handle_dispatch("C2C_MESSAGE_CREATE", event_private())
    assert len(sched.tasks) == 1, "重复 msg_id+msg_idx 应被忽略"
    print("[OK] 去重：同一 msg_id+msg_idx 只入队一次")


async def case_framework_id_from_qq_channel():
    """身份对齐 llbot：user_id 取 qq 渠道的 QQ 号，session = qq_{QQ 号}。

    规范账户是 web 侧 id（h268）时，直接拿规范 id 拼 session 会得到 qq_h268，
    与 llbot 的 qq_2195239690 不是同一段上下文 —— 必须走 qq 渠道身份。
    """
    sched = FakeScheduler()
    users = FakeUsers(
        forward={"OPENID_A": "h268"},
        channels={"h268": {"qq": "2195239690", "qqofficial": "OPENID_A"}},
    )
    bridge = OfficialBridge(sched, users, FakeOpenApi())
    await bridge.handle_dispatch("C2C_MESSAGE_CREATE", event_private())
    task = sched.tasks[0]
    assert task["user_id"] == "2195239690", task
    assert task["session_id"] == "qq_2195239690", task
    print("[OK] 身份对齐：规范账户 h268 → user_id/session 用 qq 渠道的 QQ 号")


async def case_group_map():
    sched = FakeScheduler()
    bridge = OfficialBridge(sched, FakeUsers(), FakeOpenApi())
    config.GROUP_MAP["GROUP_OPENID_X"] = "888888"
    config.GROUP_REVERSE["888888"] = "GROUP_OPENID_X"
    try:
        await bridge.handle_dispatch("GROUP_AT_MESSAGE_CREATE", event_group())
        task = sched.tasks[0]
        assert task["session_id"] == "qq_g_888888", task
        assert task["content"] == "/今日天气", task  # 首尾空格被 trim
    finally:
        config.GROUP_MAP.pop("GROUP_OPENID_X", None)
        config.GROUP_REVERSE.pop("888888", None)
    print("[OK] 群聊入站：group_map 命中 → session=qq_g_{群号}，正文已 trim")


async def case_passive_then_active():
    api = FakeOpenApi()
    bridge = OfficialBridge(FakeScheduler(), FakeUsers(), api)
    await bridge.handle_dispatch("C2C_MESSAGE_CREATE", event_private())
    for _ in range(4):
        assert await bridge.send("qq_OPENID_A", "回复") is True
    assert [c["msg_id"] for c in api.calls[:4]] == ["m1"] * 4, api.calls
    assert [c["seq"] for c in api.calls[:4]] == [1, 2, 3, 4], api.calls
    # 第 5 条超出被动额度 → 主动消息（无 msg_id）
    assert await bridge.send("qq_OPENID_A", "再来一条") is True
    assert api.calls[4]["msg_id"] == "", api.calls[4]
    print("[OK] 出站：被动回复 msg_seq 递增，额度用尽自动退化主动消息")


async def case_rotate_msg_ids():
    """官方规则是「每条用户消息可回 4 次、60 分钟内有效」：
    两条消息 = 8 个被动额度，用完才退化主动消息。"""
    api = FakeOpenApi()
    bridge = OfficialBridge(FakeScheduler(), FakeUsers(), api)
    await bridge.handle_dispatch("C2C_MESSAGE_CREATE", event_private(msg_id="m1", idx="IDX1"))
    await bridge.handle_dispatch("C2C_MESSAGE_CREATE", event_private(msg_id="m2", idx="IDX2"))
    for _ in range(8):
        assert await bridge.send("qq_OPENID_A", "回复") is True
    passive_ids = [c["msg_id"] for c in api.calls]
    assert passive_ids.count("m1") == 4, api.calls
    assert passive_ids.count("m2") == 4, api.calls
    assert set(passive_ids) == {"m1", "m2"}, api.calls
    seqs_m2 = [c["seq"] for c in api.calls if c["msg_id"] == "m2"]
    assert seqs_m2 == [1, 2, 3, 4], seqs_m2
    # 第 9 条才退化主动消息
    assert await bridge.send("qq_OPENID_A", "第九条") is True
    assert api.calls[8]["msg_id"] == "", api.calls[8]
    print("[OK] 出站：多条用户消息的被动额度轮转（4+4 次），用尽才发主动消息")


async def case_intermediate_budget():
    """中间消息额度：前 2 条立刻发，第 3 条起暂存，最终回复时合并成一条 + 最终回复本身。"""
    api = FakeOpenApi()
    bridge = OfficialBridge(FakeScheduler(), FakeUsers(), api)
    await bridge.handle_dispatch("C2C_MESSAGE_CREATE", event_private(msg_id="m1"))
    for text in ("i1", "i2"):
        assert await bridge.deliver("qq_OPENID_A", text, final=False) is True
    for text in ("i3", "i4"):
        assert await bridge.deliver("qq_OPENID_A", text, final=False) is True
    assert len(api.calls) == 2, "第 3 条起不应立即发送"

    assert await bridge.deliver("qq_OPENID_A", "FINAL", final=True) is True
    assert len(api.calls) == 4, api.calls
    assert [c["content"] for c in api.calls] == ["i1", "i2", "i3\n\ni4", "FINAL"], api.calls
    assert [c["seq"] for c in api.calls] == [1, 2, 3, 4], api.calls
    assert {c["msg_id"] for c in api.calls} == {"m1"}, api.calls
    print("[OK] 中间消息：前 2 条直发，第 3 条起暂存，最终回复时合并一条 + 最终一条（共 4 次额度）")


async def case_reserved_slot():
    """没有暂存时也要给最终回复留额度：2 条中间 + 1 条最终 = 3 次，剩 1 次不可被中间消息占用。"""
    api = FakeOpenApi()
    bridge = OfficialBridge(FakeScheduler(), FakeUsers(), api)
    await bridge.handle_dispatch("C2C_MESSAGE_CREATE", event_private(msg_id="m1"))
    await bridge.deliver("qq_OPENID_A", "i1", final=False)
    await bridge.deliver("qq_OPENID_A", "i2", final=False)
    await bridge.deliver("qq_OPENID_A", "FINAL", final=True)
    assert len(api.calls) == 3 and api.calls[2]["content"] == "FINAL", api.calls
    # 额度只剩 1 次，已被最终回复用掉；再来中间消息只能走主动消息
    await bridge.deliver("qq_OPENID_A", "i5", final=False)
    assert api.calls[3]["msg_id"] == "", api.calls[3]
    print("[OK] 额度预留：中间消息不占用留给最终回复的那 1 次被动额度")


class FakeSummarizer:
    """reader 摘要器替身：记录调用，按剧本返回。"""

    def __init__(self, summary: str = ""):
        self.summary = summary
        self.calls = []

    async def summarize(self, text: str, limit: int) -> str:
        self.calls.append({"text": text, "limit": limit})
        return self.summary


async def case_reader_summary():
    """合并后的中间消息超长时，先交给 reader 智能体压缩成一条，而不是折半重发。"""
    api = FakeOpenApi()
    summarizer = FakeSummarizer("摘要：已抓完文档，准备整理字段")
    bridge = OfficialBridge(FakeScheduler(), FakeUsers(), api, summarizer=summarizer)
    await bridge.handle_dispatch("C2C_MESSAGE_CREATE", event_private(msg_id="m1"))
    await bridge.deliver("qq_OPENID_A", "i1", final=False)
    await bridge.deliver("qq_OPENID_A", "i2", final=False)
    limit = config.REPLY_CHUNK_CHARS
    await bridge.deliver("qq_OPENID_A", "x" * (limit - 100), final=False)
    await bridge.deliver("qq_OPENID_A", "y" * (limit - 100), final=False)
    assert len(summarizer.calls) == 0, "未超长时不该调用 reader"

    await bridge.deliver("qq_OPENID_A", "FINAL", final=True)
    assert len(summarizer.calls) == 1, summarizer.calls
    assert summarizer.calls[0]["limit"] == limit
    assert len(api.calls) == 4, api.calls
    assert api.calls[2]["content"] == "摘要：已抓完文档，准备整理字段", api.calls
    assert api.calls[3]["content"] == "FINAL", api.calls
    print("[OK] reader 摘要：超长合并交给 reader 压缩，仍只占 1 次被动额度")


async def case_reader_fallback():
    """reader 不可用时退化为头尾截断，绝不因为摘要失败而丢消息。"""
    api = FakeOpenApi()
    summarizer = FakeSummarizer("")  # 返回空 = 摘要失败
    bridge = OfficialBridge(FakeScheduler(), FakeUsers(), api, summarizer=summarizer)
    await bridge.handle_dispatch("C2C_MESSAGE_CREATE", event_private(msg_id="m1"))
    await bridge.deliver("qq_OPENID_A", "i1", final=False)
    await bridge.deliver("qq_OPENID_A", "i2", final=False)
    limit = config.REPLY_CHUNK_CHARS
    await bridge.deliver("qq_OPENID_A", "x" * (limit - 100), final=False)
    await bridge.deliver("qq_OPENID_A", "y" * (limit - 100), final=False)
    await bridge.deliver("qq_OPENID_A", "FINAL", final=True)

    assert len(summarizer.calls) == 1, summarizer.calls
    merged = api.calls[2]["content"]
    assert len(merged) <= limit, len(merged)
    assert "中间消息已合并" in merged, merged[:80]
    assert len(api.calls) == 4, api.calls  # 仍只有 4 条，没有折半重发
    print("[OK] reader 兜底：摘要失败时头尾截断，消息不丢、额度不超")


async def case_command_id():
    """私聊 /id：报出 qqofficial id 并说明绑定方式，不进入任务流程。"""
    sched = FakeScheduler()
    api = FakeOpenApi()
    bridge = OfficialBridge(sched, FakeUsers(), api)
    await bridge.handle_dispatch("C2C_MESSAGE_CREATE", event_private(msg_id="c1", text="/id"))
    assert not sched.tasks, "命令不应创建任务"
    assert len(api.calls) == 1, api.calls
    reply = api.calls[0]["content"]
    assert "OPENID_A" in reply and "/bind" in reply, reply
    assert api.calls[0]["msg_id"] == "c1", api.calls  # 走被动回复
    print("[OK] 命令 /id：回复 openid + 绑定说明，不入队")


async def case_command_bind():
    """私聊 /bind <user_id>：写 user-service 绑定并回复结果。"""
    sched = FakeScheduler()
    api = FakeOpenApi()
    users = FakeUsers()
    bridge = OfficialBridge(sched, users, api)
    await bridge.handle_dispatch(
        "C2C_MESSAGE_CREATE", event_private(msg_id="c2", text="/bind 2195239690")
    )
    assert users.binds == [{"user_id": "2195239690", "channel_user_id": "OPENID_A"}], users.binds
    assert not sched.tasks
    assert "已绑定" in api.calls[0]["content"], api.calls
    print("[OK] 命令 /bind：把 openid 绑到规范 user_id，不入队")


async def case_command_bind_failed():
    """绑定冲突时把原因回给用户。"""
    api = FakeOpenApi()
    bridge = OfficialBridge(FakeScheduler(), FakeUsers(bind_ok=False), api)
    await bridge.handle_dispatch(
        "C2C_MESSAGE_CREATE", event_private(msg_id="c3", text="/bind 123")
    )
    assert "绑定失败" in api.calls[0]["content"], api.calls
    print("[OK] 命令 /bind：失败原因回显")


async def case_command_in_group():
    """群里只报 member_openid，绑定引导去私聊。"""
    api = FakeOpenApi()
    bridge = OfficialBridge(FakeScheduler(), FakeUsers(), api)
    await bridge.handle_dispatch(
        "GROUP_AT_MESSAGE_CREATE", event_group(msg_id="c4", text="/id")
    )
    reply = api.calls[0]["content"]
    assert "MEMBER_1" in reply and "私聊" in reply, reply
    print("[OK] 命令 /id（群）：报 member_openid 并引导私聊绑定")


async def case_length_split():
    api = FakeOpenApi([
        {"ok": False, "err_code": 40054007, "message": "too long"},
    ])
    bridge = OfficialBridge(FakeScheduler(), FakeUsers(), api)
    ok = await bridge.send("qq_OPENID_A", "x" * 400)
    assert ok is True
    assert len(api.calls) == 3, api.calls  # 原始 1 次失败 + 折半 2 次
    assert len(api.calls[1]["content"]) + len(api.calls[2]["content"]) == 400
    print("[OK] 出站：40054007 长度超限 → 折半重发")


async def case_stale_msg_id():
    api = FakeOpenApi([
        {"ok": False, "err_code": 40034128, "message": "passive expired"},
    ])
    bridge = OfficialBridge(FakeScheduler(), FakeUsers(), api)
    await bridge.handle_dispatch("C2C_MESSAGE_CREATE", event_private())
    ok = await bridge.send("qq_OPENID_A", "hi")
    assert ok is True
    assert len(api.calls) == 2, api.calls
    assert api.calls[1]["msg_id"] == "", api.calls[1]
    print("[OK] 出站：msg_id 失效 → 自动改发主动消息")


async def case_group_target_resolution():
    api = FakeOpenApi()
    bridge = OfficialBridge(FakeScheduler(), FakeUsers(), api)
    config.GROUP_MAP["GROUP_OPENID_X"] = "888888"
    config.GROUP_REVERSE["888888"] = "GROUP_OPENID_X"
    try:
        await bridge.send("qq_g_888888", "hello")
    finally:
        config.GROUP_MAP.pop("GROUP_OPENID_X", None)
        config.GROUP_REVERSE.pop("888888", None)
    assert api.calls[0]["kind"] == "group", api.calls
    assert api.calls[0]["target"] == "GROUP_OPENID_X", api.calls
    print("[OK] 出站：qq_g_{群号} 反解成 group_openid 投递")


async def main():
    await case_inbound_private()
    await case_dedupe()
    await case_framework_id_from_qq_channel()
    await case_group_map()
    await case_passive_then_active()
    await case_rotate_msg_ids()
    await case_intermediate_budget()
    await case_reserved_slot()
    await case_reader_summary()
    await case_reader_fallback()
    await case_command_id()
    await case_command_bind()
    await case_command_bind_failed()
    await case_command_in_group()
    await case_length_split()
    await case_stale_msg_id()
    await case_group_target_resolution()
    print("\nALL OK - 17 cases")


if __name__ == "__main__":
    asyncio.run(main())

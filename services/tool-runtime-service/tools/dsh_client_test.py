"""dsh 客户端自检：本地起一个「假插件」服务端，验证协议边界与路由。

跑法（容器内）：`python tools/dsh_client_test.py`

覆盖：
1. 地址解析复用 workspace.safe_path / mnt_access（Windows、/mnt、逃逸拒绝）
2. chat 由工作区派生；默认 chat 独立
3. `工具调用:dsh|...` 参数拆分（位置写法 + `ws=` / `perm=` 声明）
4. 协议：Bearer 头、lifecycle/heartbeat、段数组、at 段字符串、固定发信人
5. 回复：多段合并、echo 原样回传、未知 action 空应答、超时明确提示
6. 自动路由：默认 / `.` → 默认 chat（不切 cwd）；带工作区 → 独立 chat + `/workspace`
7. 绑定核对：配置 DSH_TARGET_SESSION 且走默认 chat 时发一次 `/session`
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time

sys.path.insert(0, "/app")

import websockets  # noqa: E402

PORT = 18644
TOKEN = "test-token"
DEFAULT_CHAT = "group:548018"
EXPECTED_SESSION = "session-4459dee2-cb0c-4831-9ce7-835bdb2f4d44"


class FakePlugin:
    """扮演 dsh-onebot-qq：收 message 事件，下发 send_msg action，校验 echo 回传。"""

    def __init__(self) -> None:
        self.meta: list[dict] = []
        self.messages: list[dict] = []
        self.echo_replies: list[dict] = []
        self.headers: dict[str, str] = {}
        self.silent = False
        self.queued = False
        self.parts = 2

    async def handler(self, ws) -> None:
        try:
            raw = getattr(ws, "request_headers", None)
            self.headers = dict(raw) if raw is not None else dict(ws.request.headers)
        except Exception:
            self.headers = {}

        async def reader() -> None:
            async for payload in ws:
                frame = json.loads(payload)
                if frame.get("post_type"):
                    if frame.get("post_type") == "meta_event":
                        self.meta.append(frame)
                    else:
                        self.messages.append(frame)
                        asyncio.create_task(self._respond(ws, frame))
                elif "echo" in frame:
                    self.echo_replies.append(frame)

        try:
            await reader()
        except Exception:
            pass

    async def _respond(self, ws, frame: dict) -> None:
        text = "".join(
            str(seg.get("data", {}).get("text") or "")
            for seg in frame.get("message") or []
            if isinstance(seg, dict)
        ).strip()
        target = {
            "message_type": "group",
            "group_id": frame.get("group_id"),
            "user_id": frame.get("user_id"),
        }

        if text.startswith("/session"):
            await self._action(ws, "send_msg", {
                **target,
                "message": [{"type": "text", "data": {"text": f"当前 session：{EXPECTED_SESSION}"}}],
            }, echo="session-ack")
            return
        if text.startswith("/workspace") or text.startswith("/permission"):
            await self._action(ws, "send_msg", {
                **target,
                "message": [{"type": "text", "data": {"text": f"已切换：{text}"}}],
            }, echo="switch-ack")
            return
        if self.queued:
            await self._action(ws, "send_msg", {
                **target,
                "message": [{"type": "text", "data": {
                    "text": "⏳ 当前任务运行中，新消息已排队，将在当前任务结束后依次处理。"}}],
            }, echo="queue-notice")
            return
        if self.silent:
            return
        for index in range(self.parts):
            await self._action(ws, "send_msg", {
                **target,
                "message": [{"type": "text", "data": {"text": f"第{index + 1}段回复"}}],
            }, echo=f"echo-{index + 1}")
            await asyncio.sleep(0.1)
        await self._action(ws, "get_msg", {"message_id": 1}, echo="unknown-action")

    @staticmethod
    async def _action(ws, action: str, params: dict, echo: str) -> None:
        await ws.send(json.dumps({"action": action, "params": params, "echo": echo}, ensure_ascii=False))


PASSED = 0
FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASSED
    if ok:
        PASSED += 1
        print(f"  ok    {name}")
    else:
        FAILED.append(name)
        print(f"  FAIL  {name}" + (f"   [{detail}]" if detail else ""))


def sent_texts(plugin: FakePlugin, since: int) -> list[str]:
    return [
        "".join(str(seg.get("data", {}).get("text") or "") for seg in m.get("message") or []
                if isinstance(seg, dict)).strip()
        for m in plugin.messages[since:]
    ]


def main() -> int:
    plugin = FakePlugin()
    ready = threading.Event()

    def serve() -> None:
        async def run() -> None:
            async with websockets.serve(plugin.handler, "127.0.0.1", PORT):
                ready.set()
                await asyncio.Future()

        asyncio.run(run())

    threading.Thread(target=serve, daemon=True).start()
    ready.wait(5)

    os.environ["DSH_ONEBOT_WS"] = f"ws://127.0.0.1:{PORT}/ws"
    os.environ["DSH_ONEBOT_TOKEN"] = TOKEN
    os.environ["DSH_SELF_ID"] = "10001"
    os.environ["DSH_SENDER_ID"] = "10086"
    os.environ["DSH_TARGET_SESSION"] = ""
    os.environ["DSH_PERMISSION_PRESET"] = ""
    os.environ["DSH_DEFAULT_CHAT"] = DEFAULT_CHAT
    os.environ["DSH_DEFAULT_CHAT_WORKSPACE"] = ""
    os.environ["DSH_MERGE_MILLIS"] = "400"
    os.environ["DSH_REPLY_TIMEOUT_SECONDS"] = "5"
    os.environ["WORKSPACE_DIR"] = "/app/workspace"
    os.environ["DSH_WORKSPACE_ROOT"] = "D:/my-agent/workspace"

    from app import config as app_config
    from app.dsh_client import _chat_id_for, _resolve_dsh_workspace, dsh_client
    from app.skill_runtime import SkillRuntime

    print("工作区解析（复用 workspace.safe_path / mnt_access）")
    check("Windows 路径原样接受", _resolve_dsh_workspace("D:/ws/a")[0] == "D:\\ws\\a",
          _resolve_dsh_workspace("D:/ws/a")[0])
    check("/mnt 形式转 Windows 盘符", _resolve_dsh_workspace("/mnt/d/ws/a")[0] == "D:\\ws\\a",
          _resolve_dsh_workspace("/mnt/d/ws/a")[0])
    check("容器绝对路径 → dsh 根下",
          _resolve_dsh_workspace("/app/workspace/users/2195239690",
                                 "/app/workspace/users/2195239690")[0].endswith("2195239690"),
          _resolve_dsh_workspace("/app/workspace/users/2195239690")[0])
    check("逃逸路径被拒",
          _resolve_dsh_workspace("../../etc/passwd", "/app/workspace/users/2195239690")[1] != "")
    check("空路径 → 空", _resolve_dsh_workspace("", "") == ("", ""))

    print("chat 派生")
    check("同工作区稳定", _chat_id_for("D:/ws/a") == _chat_id_for("D:/ws/a"))
    check("不同工作区不同", _chat_id_for("D:/ws/a") != _chat_id_for("D:/ws/b"))
    check("形如 group:<数字>", _chat_id_for("D:/ws/a").startswith("group:"))
    check("默认 chat 不与工作区 chat 撞",
          DEFAULT_CHAT != _chat_id_for("D:/ws/a"), f"{DEFAULT_CHAT} vs {_chat_id_for('D:/ws/a')}")

    print("参数拆分")
    split = SkillRuntime._split_dsh_args
    check("两段：工作区 + 要求", split(["D:/ws/a", "写个快排"]) == ("D:/ws/a", "", "写个快排"),
          str(split(["D:/ws/a", "写个快排"])))
    check("三段：首段工作区，其余拼要求",
          split(["D:/ws/a", "第一行", "第二行"]) == ("D:/ws/a", "", "第一行\n第二行"),
          str(split(["D:/ws/a", "第一行", "第二行"])))
    check("单段非路径 → 要求（默认 chat）", split(["写个快排"]) == ("", "", "写个快排"),
          str(split(["写个快排"])))
    check("顺序写反 → 判为 misordered",
          SkillRuntime._dsh_args_misordered(["打印 pwd", "."]) is True)
    check("正常顺序不算 misordered",
          SkillRuntime._dsh_args_misordered([".", "打印 pwd"]) is False)
    check("中文提示带斜杠不算路径", split(["读 /app 目录"]) == ("", "", "读 /app 目录"),
          str(split(["读 /app 目录"])))
    check("单段路径 → 工作区、无要求", split(["D:/ws/a"]) == ("D:/ws/a", "", ""),
          str(split(["D:/ws/a"])))
    check("`.` 位置写法 = 工作区声明", split([".", "回一句"]) == (".", "", "回一句"),
          str(split([".", "回一句"])))
    check("ws=/perm= 声明", split(["ws=D:/ws/a", "perm=danger-full-access", "干活"]) ==
          ("D:/ws/a", "danger-full-access", "干活"),
          str(split(["ws=D:/ws/a", "perm=danger-full-access", "干活"])))
    check("只声明 perm", split(["perm=workspace-noapproval", "干活"]) ==
          ("", "workspace-noapproval", "干活"),
          str(split(["perm=workspace-noapproval", "干活"])))
    check("空参数", split([]) == ("", "", ""))

    print("握手与鉴权（带工作区 → 独立会话）")
    before = len(plugin.messages)
    reply = dsh_client.ask("你好", workspace="D:/my-agent/workspace/2195239690", timeout=8)
    check("拿到回复", "第1段回复" in reply and "第2段回复" in reply, reply[:120])
    check("多段被合并", reply.count("段回复") == 2, reply[:120])
    check("authorization 头正确",
          (plugin.headers.get("authorization") or plugin.headers.get("Authorization"))
          == f"Bearer {TOKEN}",
          str(plugin.headers.get("authorization") or plugin.headers.get("Authorization")))
    check("发了 lifecycle/connect",
          any(m.get("meta_event_type") == "lifecycle" for m in plugin.meta), str(plugin.meta[:1]))
    check("发了 heartbeat", any(m.get("meta_event_type") == "heartbeat" for m in plugin.meta))
    check("meta 带 self_id", all(m.get("self_id") == 10001 for m in plugin.meta))
    check("message 用段数组", any(isinstance(m.get("message"), list) for m in plugin.messages))
    check("群消息带 at 段且 qq 是字符串",
          any(any(isinstance(seg, dict) and seg.get("type") == "at"
                  and str(seg.get("data", {}).get("qq")) == "10001"
                  for seg in m.get("message") or []) for m in plugin.messages))
    check("message_type=group", all(m.get("message_type") == "group" for m in plugin.messages))
    check("发信人固定 10086", all(m.get("user_id") == 10086 for m in plugin.messages))

    print("带工作区 → 先发 /workspace")
    check("先发 /workspace", any(t.startswith("/workspace D:\\my-agent\\workspace\\2195239690")
                                 for t in sent_texts(plugin, before)),
          str(sent_texts(plugin, before)))
    check("回复带工作区后缀", "dsh 工作区：D:\\my-agent\\workspace\\2195239690" in reply, reply[-80:])

    print("action 应答")
    deadline = time.time() + 3
    while time.time() < deadline and len(plugin.echo_replies) < 3:
        time.sleep(0.1)
    echoes = {r.get("echo") for r in plugin.echo_replies}
    check("echo 原样回传（字符串）", {"echo-1", "echo-2", "unknown-action"} <= echoes,
          str(sorted(map(str, echoes))))
    check("未知 action 回空 data",
          all(r.get("data") in ({}, {"message_id": (r.get("data") or {}).get("message_id")})
              for r in plugin.echo_replies if r.get("echo") == "unknown-action"))
    check("status/retcode 规范",
          all(r.get("status") == "ok" and r.get("retcode") == 0 for r in plugin.echo_replies))

    print("无回复")
    plugin.silent = True
    out = dsh_client.ask("这条没人回", workspace="D:/ws/quiet", timeout=2)
    check("超时给出明确提示", "dsh 无回复" in out, out[:120])
    plugin.silent = False

    print("自动路由：不带工作区 → 默认 chat")
    before = len(plugin.messages)
    out = dsh_client.ask("走默认会话", session_id="sess-plain", user_id="2195239690", timeout=6)
    texts = sent_texts(plugin, before)
    check("不发 /workspace 也不发 /permission",
          not any(t.startswith(("/workspace", "/permission")) for t in texts), str(texts))
    check("回复不带工作区后缀", "dsh 工作区" not in out, out[:80])
    check("打到默认 chat 上",
          plugin.messages[before].get("group_id") == int(DEFAULT_CHAT.split(":")[1]),
          str(plugin.messages[before].get("group_id")))

    print("自动路由：工作区是 `.` → 仍走默认 chat")
    before = len(plugin.messages)
    out = dsh_client.ask("当前工作区就行", workspace=".", timeout=6)
    texts = sent_texts(plugin, before)
    check("`.` 不发 /workspace", not any(t.startswith("/workspace") for t in texts), str(texts))
    check("`.` 不带工作区后缀", "dsh 工作区" not in out, out[:80])

    print("自动路由：带工作区 → 独立 chat + /workspace")
    before = len(plugin.messages)
    out = dsh_client.ask("去别的工作区干活", workspace="D:/other/ws", timeout=6)
    texts = sent_texts(plugin, before)
    check("发了 /workspace", any(t.startswith("/workspace D:\\other\\ws") for t in texts), str(texts))
    check("回复带工作区后缀", "dsh 工作区：D:\\other\\ws" in out, out[-90:])
    check("用的是工作区 chat 而不是默认 chat",
          plugin.messages[before].get("group_id") != int(DEFAULT_CHAT.split(":")[1]),
          str(plugin.messages[before].get("group_id")))

    print("声明式 perm=")
    before = len(plugin.messages)
    dsh_client.ask("换权限跑", workspace="D:/other/ws", preset="workspace-noapproval", timeout=6)
    texts = sent_texts(plugin, before)
    check("发了 /permission workspace-noapproval",
          any(t.startswith("/permission workspace-noapproval") for t in texts), str(texts))

    print("会话忙（插件回排队提示）")
    plugin.queued = True
    before = len(plugin.messages)
    out = dsh_client.ask("忙的时候调", timeout=6)
    check("立刻给出「会话忙」说明", "【dsh 会话忙】" in out, out[:160])
    check("说明里给出解法", "独立会话" in out, out[:200])
    plugin.queued = False

    print("默认会话切本地工作区（DSH_DEFAULT_CHAT_WORKSPACE）")
    app_config.DSH_DEFAULT_CHAT_WORKSPACE = r"D:\dsh-agent-workspace"
    before = len(plugin.messages)
    out = dsh_client.ask("默认会话跑本地目录", timeout=6)
    texts = sent_texts(plugin, before)
    check("默认 chat 发了 /workspace",
          any(t.startswith("/workspace D:\\dsh-agent-workspace") for t in texts), str(texts))
    check("回复带工作区后缀", "dsh 工作区：D:\\dsh-agent-workspace" in out, out[-90:])
    app_config.DSH_DEFAULT_CHAT_WORKSPACE = ""

    print("绑定核对（DSH_TARGET_SESSION）")
    app_config.DSH_TARGET_SESSION = EXPECTED_SESSION
    before = len(plugin.messages)
    out = dsh_client.ask("核对绑定", session_id="sess-bind", timeout=6)
    texts = sent_texts(plugin, before)
    check("默认 chat 上发了一次 /session", any(t.startswith("/session") for t in texts), str(texts))
    check("核对通过（无警告）", "【警告】" not in out, out[:120])
    app_config.DSH_TARGET_SESSION = ""

    print()
    if FAILED:
        print(f"FAILED {len(FAILED)}: {FAILED}")
        return 1
    print(f"ALL OK — {PASSED} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

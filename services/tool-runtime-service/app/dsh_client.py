"""`dsh` 工具：把 DeepSeek Harness（dsh）当成外部“大脑”，让本框架的 agent 把任务外包过去。

协议
----
`dsh-link` 那套的结论是：dsh-onebot-qq 插件是个 **OneBot v11 反向 WS 服务端**，
客户端（NapCat / 本模块）主动拨 `ws://<host>:<port>/ws`。所以这里实现一个最小可用的
**OneBot v11 反向 WS 客户端**：

1. 连接带 `Authorization: Bearer <token>`（URL 参数插件不认）；
2. 连上先发 `meta_event: lifecycle/connect`，之后每 30s 发 `heartbeat`（都带 self_id）；
3. 出站：`post_type=message` 事件，`message` 用**段数组**，`message_id` 全局唯一；
4. 入站：带 `echo` 的是插件下发的 action —— `send_msg` 取出回复文本，其余一律回空 data
   （`echo` 必须**原样回传且是字符串**，否则插件干等 30s 超时）；
5. 回复合并：收到首条后等 `silence` 秒不再来新消息才收口（长回答会被拆成多条 send_msg）。

会话与工作区
------------
- 一个 chat = 一个 dsh 会话（chatId 由我们的 session_id 派生，稳定且互不干扰）；
- 工作区：`/workspace <绝对路径>` 是插件的会话级命令（adminOnly），按 chat 持久化。
  默认把**调用者的工作区**翻译成 dsh 侧路径后切过去；也可以显式指定。
  路径解析复用既有工具：`workspace.safe_path()`（归一 + 防逃逸）、
  `mnt_access.is_windows_path()`（识别 Windows 与 /mnt 形式），
  只用 `DSH_WORKSPACE_ROOT` 一个配置表示同一工作区的 dsh 侧视图。

环境变量
--------
| 变量 | 默认 | 说明 |
|---|---|---|
| `DSH_ONEBOT_WS` | `ws://host.docker.internal:8644/ws` | dsh 侧的专用通道（别和 NapCat 抢端口） |
| `DSH_ONEBOT_TOKEN` | 空 | 与 dsh 侧 accessToken 完全一致；空则工具直接报错 |
| `DSH_SELF_ID` | `10001` | 对应 dsh 侧 botQQ |
| `DSH_SENDER_ID` | `10086` | 发信人 id，**必须写进 dsh 侧 adminUsers**（`/workspace` 仅 admin 可用） |
| `DSH_WORKSPACE_ROOT` | 空 | dsh 侧看到的同一工作区根（Windows/UNC）；留空则沿用 dsh 默认工作区 |
| `DSH_REPLY_TIMEOUT_SECONDS` | `300` | 一轮最长等多久 |
| `DSH_MERGE_SILENCE_SECONDS` | `2.5` | 多条回复的静默合并窗口 |

协议细节参考 `D:\\deepseek_workspace\\dsh-link`（接入方案 + mock-napcat）。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

from app import config
from app.workspace import safe_path

try:  # websockets 是新增依赖；缺失时给出可读错误而不是 ImportError 崩掉整个模块
    import websockets
except Exception:  # pragma: no cover - 只在没装依赖时触发
    websockets = None  # type: ignore[assignment]

CONNECT_TIMEOUT_SECONDS = 12.0
HEARTBEAT_SECONDS = 30.0
WORKSPACE_COMMAND_TIMEOUT_SECONDS = 20.0

_CQ_RE = re.compile(r"\[CQ:[^\]]*\]")


def _env(name: str, default: str = "") -> str:
    value = os.getenv(name)
    return default if value is None else value


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _segments_to_text(message: Any) -> str:
    """把 OneBot 的 message（段数组 / CQ 字符串）转成纯文本。"""
    if isinstance(message, str):
        return _CQ_RE.sub("", message).strip()
    if not isinstance(message, list):
        return ""
    parts: list[str] = []
    for segment in message:
        if not isinstance(segment, dict):
            continue
        kind = str(segment.get("type") or "")
        data = segment.get("data") or {}
        if kind == "text":
            parts.append(str(data.get("text") or ""))
        elif kind == "at":
            name = str(data.get("name") or data.get("qq") or "")
            parts.append(f"@{name}" if name else "@")
        elif kind == "image":
            parts.append("[图片]")
        elif kind == "face":
            parts.append("[表情]")
        elif kind == "record":
            parts.append("[语音]")
        elif kind:
            parts.append(f"[{kind}]")
    return "".join(parts).strip()


def _chat_id_for(workspace: str = "", session_id: str = "", user_id: str = "") -> str:
    """dsh 侧 chatId = `group:<数字>`，由**工作区**派生（无工作区时退回 session/user）。

    用群 chat 而不是私聊：私聊的 chatId 就是发信人，而发信人必须是固定的管理员
    （插件的 `/workspace` 仅 `adminUsers` 可用），二者不可兼得。
    """
    seed = (workspace or session_id or user_id or "default").strip()
    digest = 0
    for index, char in enumerate(seed):
        digest = (digest * 131 + ord(char) + index) & 0x7FFFFFFFFFFFFFFF
    return f"group:{100000 + digest % 900000}"


def _from_mnt_path(value: str) -> str:
    """复用 mnt_access.from_mnt_path（延迟导入避开循环依赖）。"""
    try:
        from app import mnt_access

        return mnt_access.from_mnt_path(value)
    except Exception:
        return ""


def _is_windows_address(value: str) -> bool:
    """是否是 Windows / UNC / WSL `/mnt` 形式的地址。

    复用 `mnt_access.is_windows_path()`（它认识 `D:/x`、`/mnt/d/x`）；
    延迟导入是为了避开 `mnt_access → skill_runtime → dsh_client` 的循环依赖。
    """
    text = str(value or "").strip()
    if text.startswith("\\\\"):
        return True
    try:
        from app import mnt_access

        return bool(mnt_access.is_windows_path(text))
    except Exception:
        return False


def _resolve_dsh_workspace(raw: str, user_workspace: Path | str = "") -> tuple[str, str]:
    """把"用户给的工作区"换到 dsh 侧路径。返回 (dsh 路径, 错误文本)。

    复用原则（不再自造前缀映射表）：
    1. 已经是 Windows / UNC / `/mnt/...` → 直接用 `mnt_access` 判定并原样返回；
    2. 否则交给 `workspace.safe_path()` 归一（容器绝对 / 外部 VM / 相对，且不许逃逸）；
    3. 取相对 `config.WORKSPACE_DIR` 的部分，拼到 `config.DSH_WORKSPACE_ROOT` 下
       —— 这正是 `workspace._vm_to_container` 的逆向。
    """
    text = str(raw or "").strip() or str(user_workspace or "").strip()
    if not text:
        return "", ""

    # 1) 已经是 dsh 侧（Windows/UNC / WSL /mnt）：转成 Windows 形态直接用。
    #    `/mnt/d/x` 这类走 mnt_access.from_mnt_path（to_mnt_path 的逆运算）
    if text.startswith("/mnt/") or text.lower().startswith("/mnt\\"):
        converted = _from_mnt_path(text)
        if converted:
            return converted, ""
    if _is_windows_address(text):
        return text.replace("/", "\\"), ""

    # 2) 归一成容器内绝对路径（复用既有解析与防逃逸）
    root = Path(user_workspace or config.WORKSPACE_DIR)
    try:
        canonical = safe_path(root, text)
    except Exception as exc:  # noqa: BLE001
        return "", f"工作区路径无法解析：{exc}"

    # 3) 换到 dsh 侧根下
    dsh_root = (config.DSH_WORKSPACE_ROOT or "").strip()
    if not dsh_root:
        return "", ""

    try:
        rel = canonical.relative_to(Path(config.WORKSPACE_DIR).resolve())
    except ValueError:
        rel = Path(canonical.name)
    suffix = str(rel).replace("/", "\\")
    return (f"{dsh_root.rstrip(chr(92))}\\{suffix}" if suffix else dsh_root), ""


def _connect_kwargs(token: str) -> dict:
    """按 websockets 版本拼连接参数。

    13.x 用 `extra_headers`，14+ 改名 `additional_headers`
    （实测 13.1 传新名会 TypeError）。
    """
    import inspect

    headers = {"Authorization": f"Bearer {token}"}
    kwargs: dict[str, Any] = {
        "open_timeout": CONNECT_TIMEOUT_SECONDS,
        "ping_interval": None,          # 心跳由 OneBot heartbeat 帧负责
        "max_size": 8 * 1024 * 1024,
    }
    try:
        params = inspect.signature(websockets.connect).parameters
    except (TypeError, ValueError):
        params = {}
    if "additional_headers" in params:
        kwargs["additional_headers"] = headers
    else:
        kwargs["extra_headers"] = headers
    return kwargs


# 插件在"当前会话正在跑回合"时回的排队提示（bridge.js QUEUE_NOTICE_TEXT）
QUEUE_NOTICE_MARKERS = ("已排队", "当前任务运行中")

# 共用"正在被 Web 使用"的会话时，dsh 侧会报模型传输失败（实测）
CONFLICT_MARKERS = ("transport failed", "DeepSeek Messages")

class _Pending:
    """一次 ask 的收集器：多条 send_msg 攒起来，静默一段时间后收口。"""

    __slots__ = ("texts", "last_at", "future", "silence")

    def __init__(self, silence: float) -> None:
        self.texts: list[str] = []
        self.last_at = 0.0
        self.future: asyncio.Future | None = None
        self.silence = silence

    def feed(self, text: str) -> None:
        if text:
            self.texts.append(text)
        self.last_at = time.monotonic()

    @property
    def joined(self) -> str:
        return "\n".join(part for part in self.texts if part).strip()


class DshClient:
    """常驻的反向 WS 客户端（单例）。多次调用复用一条连接，避免插件 last-wins 互踢。"""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._ws = None
        self._connected = False
        self._lock = threading.Lock()
        self._pending: dict[str, _Pending] = {}
        self._message_seq = 0
        self._workspace_by_chat: dict[str, str] = {}
        self._preset_by_chat: dict[str, str] = {}
        self._session_checked: set[str] = set()
        self._heartbeat_task = None

    # ── 后台事件循环 ───────────────────────────────────────────

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._loop is not None and self._thread is not None and self._thread.is_alive():
                return self._loop

            loop = asyncio.new_event_loop()
            thread = threading.Thread(target=self._run_loop, args=(loop,), daemon=True)
            thread.start()
            self._loop = loop
            self._thread = thread
            self._ready.wait(3)
            return loop

    def _run_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        asyncio.set_event_loop(loop)
        self._ready.set()
        loop.run_forever()

    # ── 连接 / 读循环 ─────────────────────────────────────────

    async def _connect(self) -> None:
        if websockets is None:
            raise RuntimeError("缺少 websockets 依赖（requirements.txt 里加 websockets）")
        if self._connected and self._ws is not None:
            return

        url = config.DSH_ONEBOT_WS
        token = config.DSH_ONEBOT_TOKEN
        if not token:
            raise RuntimeError("未配置 DSH_ONEBOT_TOKEN（必须与 dsh 侧 accessToken 一致）")

        self._ws = await websockets.connect(url, **_connect_kwargs(token))
        self._connected = True
        await self._send_meta("lifecycle")
        await self._send_meta("heartbeat")
        if self._heartbeat_task is None or self._heartbeat_task.done():
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        asyncio.create_task(self._read_loop())

    async def _heartbeat_loop(self) -> None:
        while self._connected:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            try:
                await self._send_meta("heartbeat")
            except Exception:
                return

    async def _send_meta(self, kind: str) -> None:
        if self._ws is None:
            return
        frame = {
            "post_type": "meta_event",
            "meta_event_type": kind,
            "time": int(time.time()),
            "self_id": _self_id(),
        }
        if kind == "lifecycle":
            frame["sub_type"] = "connect"
        else:
            frame["interval"] = int(HEARTBEAT_SECONDS * 1000)
        await self._ws.send(json.dumps(frame, ensure_ascii=False))

    async def _read_loop(self) -> None:
        ws = self._ws
        try:
            async for raw in ws:  # type: ignore[union-attr]
                await self._on_frame(raw)
        except Exception:
            pass
        finally:
            self._connected = False
            self._ws = None
            # 断线时把等待中的调用直接叫醒，避免干等到超时
            for pending in list(self._pending.values()):
                if pending.future and not pending.future.done():
                    pending.future.set_result("")
            self._pending.clear()

    async def _on_frame(self, raw: Any) -> None:
        try:
            frame = json.loads(raw if isinstance(raw, str) else raw.decode("utf-8", "ignore"))
        except Exception:
            return
        if not isinstance(frame, dict):
            return

        # 事件帧（我们自己发的 message 回显 / 别人的消息）——忽略
        if "post_type" in frame:
            return

        echo = frame.get("echo")
        if echo is None:
            return

        action = str(frame.get("action") or "")
        params = frame.get("params") or {}
        if action == "send_msg":
            chat_id = _chat_id_from_params(params)
            text = _segments_to_text(params.get("message"))
            pending = self._pending.get(chat_id)
            if pending is not None:
                pending.feed(text)

        # echo 必须原样回传且是字符串，否则插件会干等 30s
        data = {"message_id": self._next_message_id()} if action.startswith("send_") else {}
        reply = {"status": "ok", "retcode": 0, "data": data, "echo": echo}
        if self._ws is not None:
            try:
                await self._ws.send(json.dumps(reply, ensure_ascii=False))
            except Exception:
                pass

    # ── 出站 ─────────────────────────────────────────────────

    def _next_message_id(self) -> int:
        self._message_seq += 1
        return int(time.time() * 1000) % 1000000000 + self._message_seq

    async def _send_message(self, chat_id: str, text: str) -> None:
        """发一条群消息（群号 = 会话 key），发信人固定为配置的管理员 id。"""
        if self._ws is None:
            raise RuntimeError("未连接到 dsh")

        sender_id = _sender_id()
        self_id = _self_id()
        target = int(chat_id.split(":", 1)[1])
        frame = {
            "post_type": "message",
            "message_type": "group",
            "sub_type": "normal",
            "message_id": self._next_message_id(),
            "group_id": target,
            "user_id": sender_id,
            "message": [
                # requireMention: true 时必须 @ 机器人；at.data.qq 必须是**字符串**
                {"type": "at", "data": {"qq": str(self_id)}},
                {"type": "text", "data": {"text": f" {text}"}},
            ],
            "raw_message": f"[CQ:at,qq={self_id}] {text}",
            "time": int(time.time()),
            "self_id": self_id,
            "sender": {"user_id": sender_id, "nickname": "my-agent", "card": ""},
        }
        await self._ws.send(json.dumps(frame, ensure_ascii=False))

    async def _ask_async(
        self,
        *,
        chat_id: str,
        text: str,
        workspace: str,
        preset: str,
        allow_workspace: bool,
        timeout: float,
        silence: float,
    ) -> str:
        await self._connect()

        notes: list[str] = []

        def _note(ack: str, label: str) -> None:
            """回执里出现失败标记时记一条警告（不能静默：否则预设写错也看不出来）。"""
            low = (ack or "").lower()
            if ack and ("❌" in ack or "失败" in ack or "error" in low or "fail" in low):
                notes.append(f"{label}：{ack.strip()[:160]}")

        # 默认 chat 是按名称绑定的那条会话：不切 cwd，只在配置了期望值时核对一次绑定
        target_session = (config.DSH_TARGET_SESSION or "").strip()
        if target_session and not allow_workspace and chat_id not in self._session_checked:
            await self._send_message(chat_id, "/session")
            ack = await self._collect(
                chat_id, timeout=WORKSPACE_COMMAND_TIMEOUT_SECONDS, silence=1.0
            )
            if target_session not in (ack or ""):
                notes.append(
                    f"绑定核对：期望 {target_session}，但 dsh 回的是 {ack.strip()[:160]}"
                )
            self._session_checked.add(chat_id)

        # 请求里声明了 perm=<预设> 才切（会话级命令，发信人已在 adminUsers 里）
        if preset and self._preset_by_chat.get(chat_id) != preset:
            await self._send_message(chat_id, f"/permission {preset}")
            ack = await self._collect(
                chat_id, timeout=WORKSPACE_COMMAND_TIMEOUT_SECONDS, silence=1.0
            )
            _note(ack, f"权限预设切换到 {preset} 失败")
            self._preset_by_chat[chat_id] = preset

        # 默认 chat 也要切 cwd（配置了本地目录时）：plugin 的默认 workspacePath 是
        # UNC，pwsh 在 9P 路径上起不来（GetNamedSecurityInfoW 失败）
        if not allow_workspace and not workspace:
            default_ws = (config.DSH_DEFAULT_CHAT_WORKSPACE or "").strip()
            if default_ws:
                workspace = default_ws
        if workspace and self._workspace_by_chat.get(chat_id) != workspace:
            await self._send_message(chat_id, f"/workspace {workspace}")
            ack = await self._collect(
                chat_id, timeout=WORKSPACE_COMMAND_TIMEOUT_SECONDS, silence=1.0
            )
            _note(ack, f"工作区切换到 {workspace} 失败")
            self._workspace_by_chat[chat_id] = workspace

        pending = _Pending(silence)
        self._pending[chat_id] = pending
        try:
            await self._send_message(chat_id, text)
            merged = await self._collect(chat_id, timeout=timeout, silence=silence, pending=pending)
            if notes:
                prefix = "".join(f"【警告】{note}\n" for note in notes)
                return (prefix + merged).strip() if merged else prefix.strip()
            return merged
        finally:
            self._pending.pop(chat_id, None)

    async def _collect(
        self,
        chat_id: str,
        *,
        timeout: float,
        silence: float,
        pending: _Pending | None = None,
        drop: bool = False,
    ) -> str:
        """收满一段时间（首条后静默 silence 秒）就收口；超时返回已收到的部分。"""
        own = pending is None
        holder = pending or _Pending(silence)
        if own:
            self._pending[chat_id] = holder
        started = time.monotonic()
        try:
            while True:
                await asyncio.sleep(0.15)
                now = time.monotonic()
                if holder.texts and (now - holder.last_at) >= holder.silence:
                    break
                if (now - started) >= timeout:
                    break
            return "" if drop else holder.joined
        finally:
            if own:
                self._pending.pop(chat_id, None)

    # ── 对外同步接口 ──────────────────────────────────────────

    def ask(
        self,
        text: str,
        *,
        workspace: str = "",
        preset: str = "",
        session_id: str = "",
        user_id: str = "",
        user_workspace: Path | str = "",
        timeout: float | None = None,
    ) -> str:
        prompt = str(text or "").strip()
        if not prompt:
            return "错误：dsh 任务内容不能为空"

        # 自动路由：
        #   带工作区 → 该工作区自己的 chat（独立会话），并照常发 /workspace
        #   不带工作区 → 默认 chat（按名称绑定的那条会话，比如你在 Web 里聊的）
        raw_ws = str(workspace or "").strip()
        # `.` / `./` 是"当前工作区"的简写，等价于"没指定"→ 走默认会话（按名称绑定那条）
        declared_ws = bool(raw_ws) and raw_ws not in {".", "./", ".\\", ".\\."}
        if declared_ws:
            target_ws, ws_error = _resolve_dsh_workspace(workspace, user_workspace)
            if ws_error:
                return f"错误：{ws_error}"
            chat_id = _chat_id_for(target_ws)
        else:
            target_ws = ""
            chat_id = (config.DSH_DEFAULT_CHAT or "").strip() or _chat_id_for(
                "", session_id=session_id, user_id=user_id
            )
        # 上限语义：工具超时（可能是 600s）与配置值取小
        ceiling = float(config.DSH_REPLY_TIMEOUT_SECONDS or 300)
        if timeout and timeout > 0:
            limit = min(float(timeout), ceiling)
        else:
            limit = ceiling
        # 共用会话（默认 chat，被我们绑到你正在聊的那条）同时只能跑一轮，等太久没意义
        if not declared_ws:
            shared_cap = float(config.DSH_SHARED_CHAT_TIMEOUT_SECONDS or 0)
            if shared_cap > 0:
                limit = min(limit, shared_cap)
        silence = (config.DSH_MERGE_MILLIS or 2500) / 1000.0

        loop = self._ensure_loop()
        future = asyncio.run_coroutine_threadsafe(
            self._ask_async(
                chat_id=chat_id,
                text=prompt,
                workspace=target_ws,
                preset=str(preset or config.DSH_PERMISSION_PRESET or "").strip(),
                allow_workspace=declared_ws,
                timeout=limit,
                silence=silence,
            ),
            loop,
        )
        try:
            merged = future.result(timeout=limit + CONNECT_TIMEOUT_SECONDS + 20)
        except Exception as exc:  # noqa: BLE001 - 统一转成回执文本
            return (
                f"【dsh 调用失败】{type(exc).__name__}: {exc}\n"
                f"排查：dsh 侧是否在 {config.DSH_ONEBOT_WS} 监听、"
                "token 是否一致、是否有别的客户端（NapCat）占了同一个端口"
            )

        if not merged:
            hint = ""
            if not declared_ws:
                hint = (
                    "\n最常见原因：目标会话正被占用（你在 dsh 里正对话，一条会话同时只能跑一轮），"
                    "消息被排队了。要么等它空闲，要么带工作区走独立会话：工具调用:dsh|<工作区>|<任务>"
                )
            return (
                "【dsh 无回复】消息已发出，但在 "
                f"{limit:.0f}s 内没等到 send_msg。可能 dsh 正在跑长任务、或该 chat 未连上。"
                f"{hint}"
            )
        if any(marker in merged for marker in QUEUE_NOTICE_MARKERS):
            return (
                "【dsh 会话忙】目标会话正在跑别的回合（比如你正在 dsh 里对话），"
                "这条消息被排队了，本轮拿不到结果。\n"
                "要么等那轮结束再调，要么带工作区走独立会话：工具调用:dsh|<工作区>|<任务>"
            )
        if any(marker in merged for marker in CONFLICT_MARKERS):
            return (
                "【dsh 会话冲突】目标会话正被 Web 端占用（DSH 一条会话同时只能有一个活回合），"
                "再 resume 出第二个 agent 会报 transport failed，本轮拿不到结果。\n"
                "解法：带工作区走独立会话（工具调用:dsh|<工作区>|<任务>），"
                "或先关掉 Web 里那条对话再共用。"
            )
        effective_ws = target_ws
        if not effective_ws and not declared_ws:
            # 默认会话也可能被切到本地工作区（DSH_DEFAULT_CHAT_WORKSPACE）
            effective_ws = (config.DSH_DEFAULT_CHAT_WORKSPACE or "").strip()
        if effective_ws:
            return f"{merged}\n\n（dsh 工作区：{effective_ws}）"
        return merged

def _sender_id() -> int:
    """在 dsh 侧配置里被列为 adminUsers 的那个 id（`/workspace` 命令需要）。"""
    return int(config.DSH_SENDER_ID or 10086)


def _self_id() -> int:
    return int(config.DSH_SELF_ID or 10001)


def _chat_id_from_params(params: dict) -> str:
    if params.get("group_id"):
        return f"group:{params['group_id']}"
    return f"private:{params.get('user_id') or 0}"


dsh_client = DshClient()

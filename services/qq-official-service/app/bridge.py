"""官方 bot 的收发桥：事件 → 调度器；调度器事件 → 官方发消息接口。

入站
  C2C_MESSAGE_CREATE   → openid = d.author.user_openid
  GROUP_AT_MESSAGE_CREATE → group_openid = d.group_openid，member_openid = d.author.member_openid
  （GROUP_MESSAGE_CREATE 全量群消息默认忽略：没有 bot 自身 openid 无法确认是否 @ 了机器人）

会话 id 与 qq-llbot 保持同一套形状：
  私聊 qq_{规范user_id}（绑定前即 openid）
  群聊 qq_g_{群号}（配置了 group_map 时）或 qq_g_{group_openid}

出站
  从 session_id 反解目标，再换回本渠道地址（openid / group_openid）；
  优先被动回复（msg_id + 递增 msg_seq），窗口超限或调用失败再退化为主动消息。
"""
import asyncio
import base64
import time
from collections import OrderedDict, deque

import aiohttp

from . import config
from .session_target import parse_session_target

# 被动回复窗口（官方：单聊 60 分钟 / 4 次；群聊 5 分钟 / 5 次）
PASSIVE_WINDOW = {"private": 60 * 60, "group": 5 * 60}
PASSIVE_BUDGET = {"private": 4, "group": 5}

# 中间消息最多用掉 3 次被动额度，固定留 1 次给最终回复：
#   前 MAX_IMMEDIATE_INTERMEDIATE 条立刻发；更靠后的全部暂存，最终回复时合并成一条。
MAX_IMMEDIATE_INTERMEDIATE = 2
RESERVED_PASSIVE = 1

# 同一 msg_id 的回复序号
_ACTIVE_SKIP_CODES = {40034128, 40034005, 40034024}  # 被动窗口/msg_id 失效
_LENGTH_CODES = {40054007, 40054018}  # 内容超长
_DUPLICATE_CODE = 40054005  # msg_id+msg_seq 重复


class OfficialBridge:
    def __init__(self, scheduler, user_client, openapi, channel: str | None = None, summarizer=None):
        self.scheduler = scheduler
        self.users = user_client
        self.openapi = openapi
        # reader 智能体摘要器（可选）；没有就退化为本地头尾截断
        self.summarizer = summarizer
        self.channel = channel or config.BINDING_CHANNEL

        self._seen: "OrderedDict[tuple[str, str], float]" = OrderedDict()
        # (kind, address) → 最近几条用户消息的被动额度；每条各有 PASSIVE_BUDGET 次
        self._passive: dict[tuple[str, str], deque] = {}
        self._seq: dict[str, int] = {}
        # session → {"sent": 已立即发出的中间消息数, "pending": 暂存的中间消息}
        self._turn: dict[str, dict] = {}

    # ── 去重 ────────────────────────────────────────────────
    def _is_duplicate(self, msg_id: str, idx: str) -> bool:
        key = (msg_id, idx)
        now = time.time()
        # 清理 10 分钟前的记录
        while self._seen:
            oldest = next(iter(self._seen.items()))
            if now - oldest[1] < 600:
                break
            self._seen.popitem(last=False)
        if key in self._seen:
            return True
        self._seen[key] = now
        return False

    @staticmethod
    def _scene_idx(data: dict) -> str:
        scene = data.get("message_scene") or {}
        ext = scene.get("ext") or []
        for item in ext:
            text = str(item)
            if text.startswith("msg_idx="):
                return text[len("msg_idx="):]
        return ""

    # ── 入站 ────────────────────────────────────────────────
    async def handle_dispatch(self, event_type: str, data: dict):
        if event_type == "C2C_MESSAGE_CREATE":
            await self._on_private(data)
        elif event_type == "GROUP_AT_MESSAGE_CREATE":
            await self._on_group(data)
        # 其他事件（FRIEND_ADD、GROUP_ADD_ROBOT…）暂不进入任务流程

    async def _on_private(self, data: dict):
        author = data.get("author") or {}
        openid = str(author.get("user_openid") or "").strip()
        if not openid:
            return
        msg_id = str(data.get("id") or "")
        if msg_id and self._is_duplicate(msg_id, self._scene_idx(data)):
            print(f"[qq-official] 重复推送已忽略 msg_id={msg_id}", flush=True)
            return

        canonical = await self.users.resolve_canonical(openid)
        # 框架身份以**qq 渠道**的 id（QQ 号）为准：llbot 的 user_id/session 就是这么拼的
        # （user_id = QQ 号，session = qq_{QQ 号}），只有这样 openviking 才读到同一段上下文。
        framework_id = await self._framework_id(canonical)
        session_id = f"qq_{framework_id}"
        self._turn.pop(session_id, None)
        content, images = await self._payload(data)
        if not content and images:
            content = "[图片]"
        if not content and not images:
            return
        if msg_id:
            self._record_passive("private", openid, msg_id)
        # /id、/bind 是渠道自带的运维命令，不进任务流程
        if await self._handle_command("private", openid, content, msg_id):
            return
        await self._submit(framework_id, session_id, content, images, msg_id)

    async def _on_group(self, data: dict):
        group_openid = str(data.get("group_openid") or "").strip()
        author = data.get("author") or {}
        member = str(author.get("member_openid") or "").strip()
        if not group_openid:
            return
        msg_id = str(data.get("id") or "")
        if msg_id and self._is_duplicate(msg_id, self._scene_idx(data)):
            print(f"[qq-official] 重复推送已忽略 msg_id={msg_id}", flush=True)
            return

        group_key = config.GROUP_MAP.get(group_openid, group_openid)
        session_id = f"qq_g_{group_key}"
        self._turn.pop(session_id, None)
        # 群发言人同样用 qq 渠道身份（QQ 号），与 llbot 的 user_id 对齐
        speaker = await self.users.resolve_canonical(member) if member else member
        user_id = (await self._framework_id(speaker)) if speaker else speaker
        content, images = await self._payload(data)
        if not content and images:
            content = "[图片]"
        if not content and not images:
            return
        if msg_id:
            self._record_passive("group", group_openid, msg_id)
        if await self._handle_command("group", group_openid, content, msg_id, member=member):
            return
        await self._submit(user_id or member, session_id, content, images, msg_id)

    async def _framework_id(self, canonical: str) -> str:
        """规范账户 → 框架里用的 user_id。

        llbot 的规则是 `user_id = QQ 号`、`session_id = qq_{QQ 号}`；
        官方渠道拿不到 QQ 号，所以从 user-service 的 `qq` 渠道绑定里取，
        取不到（没在 llbot 用过）才退回规范 id/openid。
        """
        if not canonical:
            return canonical
        qq_id = await self.users.channel_user_id(canonical, "qq")
        return qq_id or canonical

    async def _handle_command(
        self,
        kind: str,
        address: str,
        content: str,
        msg_id: str,
        member: str = "",
    ) -> bool:
        """处理 /id、/bind 等渠道命令。返回 True 表示"已应答，不进入任务流程"。

        openid 是渠道身份，用户只能从机器人自己的回复里拿到，所以必须由机器人报出来；
        绑定则直接写 user-service（与 dashboard 的「QQ 设置」写的是同一份数据）。
        """
        text = (content or "").strip()
        if not text.startswith("/"):
            return False
        command, _, arg = text.partition(" ")
        command = command.strip().lower()
        arg = arg.strip()

        if command not in ("/id", "/bind"):
            return False

        if kind == "group":
            await self._reply(
                kind,
                address,
                f"群内 id（member_openid，仅本群有效）：{member}\n"
                "绑定请私聊机器人发送 /bind <user_id>，群里绑不了。",
            )
            return True

        if command == "/id":
            bound = await self.users.channel_user_id(address, config.BINDING_CHANNEL)
            state = f"当前已绑定到 {bound}" if bound else "当前未绑定（会话 id 为 qq_{openid}）"
            await self._reply(
                kind,
                address,
                f"qqofficial id: {address}\n{state}\n"
                "绑定：/bind <user_id>（一般就是你的 QQ 号），绑定后与 LLBot 共用上下文。",
            )
            return True

        if not arg:
            await self._reply(kind, address, "用法：/bind <user_id>，例如 /bind 2195239690")
            return True
        ok, message = await self.users.bind(arg, address)
        if ok:
            await self._reply(kind, address, f"已绑定：{arg} ↔ {address}\n之后的对话会与 LLBot 共用同一段上下文。")
        else:
            await self._reply(kind, address, f"绑定失败：{message}")
        return True

    async def _reply(self, kind: str, address: str, text: str) -> None:
        """命令应答直接走被动回复（此时刚收到消息，额度一定够）。"""
        ok = await self._send_chunk(kind, address, text, depth=0, reserve=0)
        if not ok:
            print(f"[qq-official] 命令回复失败 kind={kind} target={address}", flush=True)

    async def _submit(self, user_id: str, session_id: str, content: str, images: list[str], msg_id: str):
        result = await self.scheduler.create_task(
            user_id=user_id,
            content=content,
            channel=config.ROUTE_CHANNEL,
            session_id=session_id,
            client_message_id=msg_id,
            images=images,
        )
        if result.get("ok"):
            print(
                f"[qq-official] 任务已提交 user={user_id} session={session_id} "
                f"task_id={result.get('task_id')}",
                flush=True,
            )
        else:
            print(f"[qq-official] 提交失败 session={session_id}: {result.get('error')}", flush=True)
            await self.send(session_id, f"提交失败：{result.get('error')}", [])

    async def _payload(self, data: dict) -> tuple[str, list[str]]:
        content = str(data.get("content") or "").strip()
        images: list[str] = []
        for item in data.get("attachments") or []:
            if not isinstance(item, dict):
                continue
            ctype = str(item.get("content_type") or "")
            url = str(item.get("url") or "")
            if url and ctype.startswith("image/"):
                data_url = await self._download_image(url)
                if data_url:
                    images.append(data_url)
        if not content:
            # 卡片/引用等消息正文可能为空，用卡片摘要兜底，避免整条丢弃
            ark = data.get("ark_data")
            if isinstance(ark, dict):
                content = str(ark.get("prompt") or ark.get("fields", {}).get("title") or "").strip()
        return content, images

    async def _download_image(self, url: str) -> str:
        timeout = aiohttp.ClientTimeout(total=20)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        return ""
                    raw = await resp.read()
                    if not raw or len(raw) > 8 * 1024 * 1024:
                        return ""
                    mime = resp.headers.get("content-type") or "image/png"
                    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"
        except Exception as exc:
            print(f"[qq-official] 图片下载失败: {exc}", flush=True)
            return ""

    # ── 出站 ────────────────────────────────────────────────
    async def resolve_target(self, session_id: str) -> tuple[str, str] | None:
        parsed = parse_session_target(session_id)
        if parsed is None:
            return None
        kind, ident = parsed
        if kind == "group":
            # session 里可能是群号；换回 group_openid
            return ("group", config.GROUP_REVERSE.get(ident, ident))
        # 私聊：可能是规范 user_id（如 QQ 号），也可能是未绑定的 openid
        openid = await self.users.channel_user_id(ident, config.BINDING_CHANNEL)
        return ("private", openid or ident)

    async def deliver(self, session_id: str, text: str, images: list[str] | None = None, final: bool = False) -> bool:
        """统一投递入口：区分中间消息与最终回复。

        官方被动额度是「每条用户消息 4 次」，主动消息又很贵，所以按下面的规则花：
          - 中间消息：前 2 条有需要就立刻发（各占 1 次，且每条 msg_id 留 1 次不给中间消息）
          - 从第 3 条中间消息起：**全部暂存**，等最终回复时合并成一条发出
          - 最终回复：先发合并后的中间消息（若有），再单独发最终回复 —— 两条
        这样一条用户消息最多消耗 3 次被动额度，最后 1 次固定留给最终回复。
        """
        if final:
            return await self._deliver_final(session_id, text, images)
        return await self._deliver_intermediate(session_id, text, images)

    async def _deliver_intermediate(self, session_id: str, text: str, images: list[str] | None = None) -> bool:
        state = self._turn.setdefault(session_id, {"sent": 0, "pending": []})
        text = (text or "").strip()
        if not text:
            return True
        if state["sent"] < MAX_IMMEDIATE_INTERMEDIATE:
            # 中间消息也留 1 次额度给最终回复
            if await self._send(session_id, text, images, reserve=1):
                state["sent"] += 1
                return True
            # 没发出去（例如窗口已过）：转暂存，跟着最终回复一起发，别丢内容。
            # 这里仍回报"已接收"——内容由本服务持有，不需要调度器重投。
            state["pending"].append(text)
            print(
                f"[qq-official] 中间消息即时发送失败，已转暂存 session={session_id}",
                flush=True,
            )
            return True
        state["pending"].append(text)
        return True  # 暂存视为已接收，稍后合并投递

    async def _deliver_final(self, session_id: str, text: str, images: list[str] | None = None) -> bool:
        state = self._turn.pop(session_id, {"sent": 0, "pending": []})
        ok = True
        pending = [item for item in state["pending"] if (item or "").strip()]
        if pending:
            merged = await self._compose_pending(session_id, pending)
            ok = await self._send(session_id, merged, None, reserve=0)
            print(
                f"[qq-official] 合并发送 {len(pending)} 条中间消息 session={session_id} "
                f"chars={len(merged)}",
                flush=True,
            )
        ok_final = await self._send(session_id, text, images, reserve=0)
        return ok and ok_final

    async def _compose_pending(self, session_id: str, pending: list[str]) -> str:
        """合并暂存的中间消息：超长时先让 reader 智能体压缩，失败才本地截断。"""
        limit = int(config.REPLY_CHUNK_CHARS or 1500)
        merged = "\n\n".join(pending)
        if len(merged) <= limit:
            return merged
        if self.summarizer is not None:
            summary = await self.summarizer.summarize(merged, limit)
            if summary:
                print(
                    f"[qq-official] reader 摘要完成 session={session_id} "
                    f"{len(merged)} -> {len(summary)} 字",
                    flush=True,
                )
                return summary
        return self._merge_pending(pending, limit)

    @staticmethod
    def _merge_pending(pending: list[str], size: int | None = None) -> str:
        """把暂存的中间消息合并成**一条**，保证不超过单条上限，避免再多消耗额度。

        超长时保留头尾、中间用省略标记概括 —— 中间消息本来就是过程信息，
        丢掉中段比多花掉一次被动额度（或退化成主动消息）划算。
        """
        limit = int(size or config.REPLY_CHUNK_CHARS or 1500)
        merged = "\n\n".join(pending)
        if len(merged) <= limit:
            return merged

        marker_template = "\n…（中间消息已合并，省略 {omitted} 字）…\n"
        # 省略标记里的数字位数会影响总长度，预留 8 字再按实际情况收紧
        placeholder_len = len(marker_template.replace("{omitted}", "000000"))
        budget = max(limit - placeholder_len, 200)
        head_len = budget // 2
        tail_len = budget - head_len
        while head_len > 100:
            omitted = max(len(merged) - head_len - tail_len, 0)
            marker = marker_template.format(omitted=omitted)
            out = merged[:head_len] + marker + merged[len(merged) - tail_len:]
            if len(out) <= limit:
                return out
            excess = len(out) - limit
            head_len = max(head_len - excess, 100)
            tail_len = max(budget - head_len, 100)
        return merged[:limit]

    async def send(self, session_id: str, text: str, images: list[str] | None = None, reserve: int = 0) -> bool:
        return await self._send(session_id, text, images, reserve)

    async def _send(self, session_id: str, text: str, images: list[str] | None = None, reserve: int = 0) -> bool:
        target = await self.resolve_target(session_id)
        if target is None:
            print(f"[qq-official] 无法解析投递目标 session={session_id}", flush=True)
            return False
        kind, address = target
        if not address:
            print(f"[qq-official] 投递地址为空 session={session_id}", flush=True)
            return False
        return await self._send_with_target(kind, address, text, images, reserve=reserve)

    async def _send_with_target(
        self,
        kind: str,
        address: str,
        text: str,
        images: list[str] | None = None,
        reserve: int = 0,
    ) -> bool:
        """已解析出 (kind, address) 之后的真正发送：先文本，再逐张图片。"""
        text = (text or "").strip()
        delivered = False

        # 先发文本（纯图片消息时 text 为空，跳过）
        if text:
            chunks = self._split(text, config.REPLY_CHUNK_CHARS)
            for chunk in chunks:
                ok = await self._send_chunk(kind, address, chunk, depth=0, reserve=reserve)
                delivered = delivered or ok

        # 再发图片（媒体上传 + msg_type=7）
        failures: list[tuple[str, str]] = []
        for index, image in enumerate(images or [], 1):
            ok, reason = await self._send_image(
                kind, address, image, index=index, reserve=reserve
            )
            delivered = delivered or ok
            if not ok:
                failures.append((str(image), reason))

        # 图片发不出去时，把失败原因显式告诉用户（不让图"悄悄消失"）
        if failures:
            notice_lines = ["【图片发送失败】官方渠道没能发出图片："]
            for image, reason in failures:
                notice_lines.append(f"- {reason}")
                notice_lines.append(f"  图片链接：{image}")
            notice = chr(10).join(notice_lines)
            try:
                await self._send_chunk(kind, address, notice, depth=0, reserve=reserve)
                delivered = True
            except Exception as exc:  # noqa: BLE001
                print(f"[qq-official] 发送图片失败提示也失败了: {exc}", flush=True)

        if not text and not images:
            return True  # 既无正文也无图片，视为已处理
        # 只要用户收到了内容（正文或失败提示）就算投递成功，避免重复重投
        return delivered

    @staticmethod
    def _split(text: str, size: int) -> list[str]:
        size = max(int(size or 0), 200)
        if len(text) <= size:
            return [text]
        return [text[i:i + size] for i in range(0, len(text), size)]

    def _next_seq(self, msg_id: str) -> int:
        seq = self._seq.get(msg_id, 0) + 1
        self._seq[msg_id] = seq
        if len(self._seq) > 200:
            self._seq.pop(next(iter(self._seq)))
        return seq

    def _record_passive(self, kind: str, address: str, msg_id: str):
        """记一条用户消息的被动额度：每条消息各有 PASSIVE_BUDGET 次，窗口滚动。"""
        if not msg_id:
            return
        bucket = self._passive.setdefault((kind, address), deque(maxlen=4))
        bucket.append({"msg_id": msg_id, "at": time.time(), "used": 0})

    def _passive_slot(self, kind: str, address: str, reserve: int = 0) -> tuple[str, int] | None:
        """挑一条还有剩余额度的用户消息（新的优先），返回 (msg_id, seq)。"""
        bucket = self._passive.get((kind, address))
        if not bucket:
            return None
        window = PASSIVE_WINDOW.get(kind, 300)
        budget = PASSIVE_BUDGET.get(kind, 4)
        now = time.time()
        while bucket and now - bucket[0]["at"] > window:
            bucket.popleft()  # 过期的旧消息直接淘汰
        usable = max(budget - max(int(reserve), 0), 0)
        for rec in reversed(bucket):
            if now - rec["at"] > window or rec["used"] >= usable:
                continue
            msg_id = str(rec["msg_id"])
            rec["used"] += 1
            return msg_id, self._next_seq(msg_id)
        return None

    async def _send_chunk(self, kind: str, address: str, text: str, depth: int, reserve: int = 0) -> bool:
        slot = self._passive_slot(kind, address, reserve)
        msg_id, seq = ("", 0) if slot is None else slot

        result = await self.openapi.send_message(kind, address, text, msg_id, seq)
        if result.get("ok"):
            return True

        err = result.get("err_code")
        # msg_id/序号失效 → 直接改发主动消息
        if err in _ACTIVE_SKIP_CODES or err == _DUPLICATE_CODE:
            result = await self.openapi.send_message(kind, address, text)
            if result.get("ok"):
                return True
            err = result.get("err_code")

        # 内容超长 → 折半重试
        if err in _LENGTH_CODES and depth < 2 and len(text) > 200:
            mid = len(text) // 2
            left = await self._send_chunk(kind, address, text[:mid], depth + 1)
            right = await self._send_chunk(kind, address, text[mid:], depth + 1)
            return left or right

        print(
            f"[qq-official] 发送失败 kind={kind} err={err} "
            f"http={result.get('http_status')} message={result.get('message')}",
            flush=True,
        )
        return False

    async def _fetch_image_bytes(self, image: str) -> tuple[bytes, str]:
        """取图片字节：data URL / http(s) / 本地路径。返回 (bytes, 来源说明)。"""
        raw = str(image or "").strip()
        if not raw:
            return b"", "图片地址为空"
        if raw.startswith("data:"):
            try:
                _, _, payload = raw.partition(",")
                return base64.b64decode(payload), "data URL"
            except Exception as exc:  # noqa: BLE001
                return b"", f"data URL 解码失败: {exc}"
        if raw.startswith(("http://", "https://")):
            timeout = aiohttp.ClientTimeout(total=config.IMAGE_FETCH_TIMEOUT_SECONDS)
            last = ""
            # 图床刚发布完可能有极短的可见性延迟，重试一次；404 也重试，第二次仍失败再报错
            for attempt, delay in enumerate((0.0, 0.6), start=1):
                if delay:
                    await asyncio.sleep(delay)
                try:
                    async with aiohttp.ClientSession(timeout=timeout) as session:
                        async with session.get(raw) as resp:
                            if resp.status == 200:
                                return await resp.read(), "http"
                            last = f"下载失败 HTTP {resp.status}"
                except Exception as exc:  # noqa: BLE001
                    last = f"下载失败: {exc}"
                print(f"[qq-official] 取图第 {attempt} 次失败（{last}）：{raw[:100]}", flush=True)
            return b"", last
        try:
            from pathlib import Path

            path = Path(raw)
            if path.is_file():
                return path.read_bytes(), "本地文件"
            return b"", f"本地文件不存在: {raw}"
        except Exception as exc:  # noqa: BLE001
            return b"", f"读取失败: {exc}"

    async def _send_image(
        self, kind: str, address: str, image: str, index: int = 1, reserve: int = 0
    ) -> tuple[bool, str]:
        """发一张图：取字节 → 上传媒体（file_data）→ msg_type=7。

        返回 (ok, 失败原因)。失败原因会回给用户（见 _send_with_target），
        免得图片"悄悄消失"。
        """
        data, reason = await self._fetch_image_bytes(image)
        if not data:
            print(f"[qq-official] 图片 #{index} 取字节失败（{reason}）：{image[:100]}", flush=True)
            return False, f"取图失败：{reason}"

        upload = await self.openapi.upload_media(kind, address, data, file_type=1)
        if not upload.get("ok"):
            detail = (
                f"上传失败 err={upload.get('err_code')} "
                f"http={upload.get('http_status')} {upload.get('message') or ''}"
            ).strip()
            print(
                f"[qq-official] 图片 #{index} {detail}（{len(data)} 字节，来源 {reason}）",
                flush=True,
            )
            return False, detail

        slot = self._passive_slot(kind, address, reserve)
        msg_id, seq = ("", 0) if slot is None else slot
        result = await self.openapi.send_media_message(
            kind, address, upload["file_info"], msg_id, seq
        )
        if result.get("ok"):
            print(f"[qq-official] 图片 #{index} 已发送（{len(data)} 字节，来源 {reason}）", flush=True)
            return True, ""

        err = result.get("err_code")
        if err in _ACTIVE_SKIP_CODES or err == _DUPLICATE_CODE:
            result = await self.openapi.send_media_message(kind, address, upload["file_info"])
            if result.get("ok"):
                print(f"[qq-official] 图片 #{index} 已发送（主动消息）", flush=True)
                return True, ""
            err = result.get("err_code")

        detail = (
            f"发送失败 err={err} http={result.get('http_status')} "
            f"{result.get('message') or ''}"
        ).strip()
        print(f"[qq-official] 图片 #{index} {detail}", flush=True)
        return False, detail

    async def is_online(self) -> bool:
        """官方渠道没有"在线"概念：能拿到 token 就认为可达。"""
        return bool(await self.openapi.tokens.get())

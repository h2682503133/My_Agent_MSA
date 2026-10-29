"""未送达事件的投递、确认、跨渠道转投与缓冲。

为什么需要「确认」
------------------
调度器过去把「事件进到订阅者队列」当成「已送达」：

    for subscriber_id, (channels, q) in items:
        if channels and event_channel not in channels:
            continue
        q.put_nowait(event)      # 入队成功 = 已送达？
        return

但**订阅者的存活与渠道的可用性是两回事**。qq-llbot 订阅调度器的 gRPC 流和它的 Satori
连接是两条独立通道：Satori 断了（实测反复 `Cannot connect to host host.docker.internal:5600`），
gRPC 订阅依然在、`put_nowait` 依然成功 —— 于是事件被"吃掉"，跨渠道转投与缓冲成为
**永远走不到的死代码**，用户侧表现就是「QQ 离线时消息完全没反应」。

修法不是让消费端退订表达渠道存活（web 侧结构上做不到：网关只有一条订阅流覆盖所有
web 用户，不能为某个用户离线而退订），而是**逐事件确认**：消费端 ack 投递结果，
调度器据此决定转投 / 缓冲。

投递状态机
----------
    发布 ──有订阅者──> 入队 + 登记 inflight ──ack ok──> 完成
          │                                 └─ack 失败 / 超时─┐
          └──无订阅者──────────────────────────────────────────┤
                                                                ▼
                                              跨渠道转投（按用户渠道优先级）
                                                                │全部失败
                                                                ▼
                                                          缓冲（等渠道路径恢复）
                                                                │
                        FlushBuffered（SSE 建立 / Satori 重连）──┘

只有「用户可见事件」需要 ack（`is_ack_required`），过程类事件仍是一次性投递，
避免为噪音事件增加往返。事件带 `metadata._ack_required=1` 告知消费端该回哪一条。
"""

import os
import queue
import threading
import time
import uuid
from typing import Iterable

from app.scheduled_task import ScheduledTask

MAX_BUFFER_PER_USER = 200

# 订阅流心跳间隔（秒）：subscribe() 每隔该时间 yield 一次 None（心跳），
# 让上层（server.SubscribeEvents）有机会检查 context.is_active()。
# 必须存在：否则客户端断开后 handler 会永久阻塞在 q.get() 上，
# 逐个泄漏 gRPC 工作线程，最终线程池耗尽导致 CreateTask 等 RPC 全部排队卡死。
# 只影响"客户端已断开"的检测延迟，不影响正常事件投递（事件到达即立即 yield）。
SUBSCRIBE_HEARTBEAT_SECONDS = 10.0

# ─── 投递确认 ────────────────────────────────────────────────
# 需要 ack 的终态事件类型（丢了用户就会静默无响应）
ACK_REQUIRED_TYPES = {
    "task_waiting_user",
    "task_failed",
    "task_timeout",
    "task_cancelled",
    "task_error",
    "task_finished_with_error",
}
# ack 超时（秒）：消费端崩溃/卡死时永远等不到 ack，超时后按投递失败处理。
# 必须大于消费端一次发送的合理耗时（QQ 推送 + 网络抖动）。
ACK_TIMEOUT_SECONDS = float(os.getenv("ACK_TIMEOUT_SECONDS", "20"))
ACK_SWEEP_INTERVAL_SECONDS = 1.0
# 跨渠道转投的最大跳数（防止多渠道之间来回弹）
MAX_FALLBACK_HOPS = int(os.getenv("MAX_FALLBACK_HOPS", "3"))
# 同一条消息最多被缓冲几次；超出即丢弃。
# 没有这个上限的话，"回放 → 投递失败 → 回到缓冲"会变成死循环。
MAX_BUFFER_ATTEMPTS = int(os.getenv("MAX_BUFFER_ATTEMPTS", "5"))

_META_ACK_REQUIRED = "_ack_required"
_META_FALLBACK_FROM = "_fallback_from"
_META_FALLBACK_HOPS = "_fallback_hops"
_META_TRIED_CHANNELS = "_tried_channels"
_META_BUFFER_ATTEMPTS = "_buffer_attempts"
# 该事件**不允许**通过这些渠道投递（例如官方 bot 的主动消息受限 → 定时任务屏蔽 qqofficial）
_META_SUPPRESS_CHANNELS = "suppress_channels"


def is_ack_required(event: dict) -> bool:
    """该事件是否要求消费端回报投递结果。

    只有用户可见事件需要：assistant_message / assistant_intermediate 且
    `visible_to_user=true`，以及各类失败终态与询问挂起。
    """
    etype = str(event.get("type") or "")
    if etype in ("assistant_message", "assistant_intermediate"):
        meta = event.get("metadata") or {}
        return str(meta.get("visible_to_user") or "").strip().lower() == "true"
    return etype in ACK_REQUIRED_TYPES


def _meta(event: dict) -> dict:
    meta = event.get("metadata")
    return meta if isinstance(meta, dict) else {}


def suppressed_channels(event: dict) -> set[str]:
    """事件显式屏蔽的渠道名集合（逗号分隔，来自 metadata.suppress_channels）。

    用途：官方 QQ bot 的主动消息受限 → 定时任务不应该再走 qqofficial，
    这类事件在 publish / 转投时都会跳过被屏蔽的渠道。
    """
    raw = str(_meta(event).get(_META_SUPPRESS_CHANNELS) or "")
    return {item.strip() for item in raw.split(",") if item.strip()}


def _brief(event: dict) -> str:
    """日志用的短标识：事件类型 / 渠道 / 用户 / 会话。"""
    return (
        f"id={event.get('event_id')} type={event.get('type')} "
        f"channel={event.get('channel')} user={event.get('user_id')} "
        f"session={event.get('session_id')}"
    )


def _log(msg: str) -> None:
    """投递链路日志。

    这条链路横跨调度器/网关/qq-llbot 三个服务，且"投递失败"过去是静默的 ——
    没有日志就无法判断消息停在哪一环（实测排查时正是靠网关侧日志才定位到
    session_id 未重建的问题）。
    """
    print(f"[event-bus] {msg}", flush=True)



def _channel_session_id(channel: str, channel_user_id: str, fallback: str) -> str:
    """跨渠道转投时的会话 id：按**目标渠道**的约定重建。

    会话 id 是渠道私有的推送地址：
      - qq 私聊 `qq_{QQ号}`（`qq-llbot._session_key`）
      - web     `web_{user_id}`（`gateway.build_session_id`）

    两者都由同一条通用规则覆盖：`{channel}_{channel_user_id}`。

    **必须重建**：只改写 channel / user_id 而不改 session_id 时，qq-llbot 会收到
    `web_xxx` 这种它无法解析的会话 id —— 实测会打印"无法解析会话目标，放弃推送"，
    即 Satori 在线时这条转投仍然送不到 QQ。

    （群聊不在渠道绑定模型里：绑定是按"人"的，转投目标只能是私聊。）
    """
    if channel and channel_user_id:
        return f"{channel}_{channel_user_id}"
    return fallback


class EventBus:
    def __init__(self):
        self._lock = threading.Lock()
        self._subscribers: dict[str, tuple[set[str], queue.Queue]] = {}
        # 未送达事件 buffer: user_id -> [event]
        self._buffer: dict[str, list[dict]] = {}
        # user_client 由外部注入
        self._user_client = None
        # 在途（已入队但未确认）: event_id -> record
        self._inflight: dict[str, dict] = {}
        self._sweeper_started = False

    def set_user_client(self, client):
        self._user_client = client

    # ─── 渠道解析 ────────────────────────────────────────────

    def _channel_targets(self, user_id: str) -> list[tuple[str, str, int]]:
        """[(channel, channel_user_id, priority)]；失败返回 []。

        user-service 对渠道身份（QQ 号）透明跟随别名，所以直接用事件里的 user_id 查即可。
        """
        if self._user_client is None or not user_id:
            return []
        getter = getattr(self._user_client, "get_channel_targets", None)
        if getter is None:
            return []
        try:
            return list(getter(user_id))
        except Exception:
            return []

    def _get_user_channels(self, user_id: str) -> set[str]:
        return {channel for channel, _cid, _prio in self._channel_targets(user_id)}

    def _match_subscribers(self, items, channel: str, exclude: str = "", exclude_set=None):
        """能接收该渠道的所有订阅者，按**优先级降序**（同优先级保持注册顺序）。

        统一 `qq` 渠道后，llbot 与 qqofficial 都订阅 `qq`：优先级决定"只投一个"时投谁
        （llbot 高、qqofficial 低 —— 后者作兜底，因为官方被动窗口易失效）。
        """
        exclude_set = exclude_set or set()
        matched = []
        for order, (subscriber_id, entry) in enumerate(items):
            channels, q = entry[0], entry[1]
            priority = int(entry[2]) if len(entry) > 2 else 0
            if exclude and subscriber_id == exclude:
                continue
            if subscriber_id in exclude_set:
                continue
            if channels and channel not in channels:
                continue
            matched.append((-priority, order, subscriber_id, q))
        matched.sort()
        return [(subscriber_id, q) for _prio, _order, subscriber_id, q in matched]

    def _match_subscriber(self, items, channel: str, exclude: str = ""):
        """找能接收该渠道的订阅者（channels 为空 = 全收）。优先级最高者。"""
        found = self._match_subscribers(items, channel, exclude=exclude)
        return found[0] if found else None

    # ─── 订阅 ───────────────────────────────────────────────

    def subscribe(self, subscriber_id: str, channels: Iterable[str], priority: int = 0):
        channel_set = set(channels or [])
        q: queue.Queue = queue.Queue(maxsize=500)
        with self._lock:
            self._subscribers[subscriber_id] = (channel_set, q, int(priority or 0))
        # 这里**不再回放缓冲**。
        #
        # 早先的实现会把 buffer 里"该用户任一渠道匹配本订阅"的事件原样塞进队列。
        # 那是错的：缓冲事件带着**上一次尝试渠道**的 channel / user_id / session_id，
        # 原样交给另一个渠道的订阅者后必然投不出去（实测：网关收到 user=999000222 的
        # 事件、按 QQ 身份找 web 的 SSE 落空、回报 unknown，事件继续躺在缓冲里空转）。
        # 回放统一走 `flush_buffered`：它按目标渠道重建身份与会话 id、校验可达性、
        # 并重新登记在途，语义完整。
        _log(
            f"subscriber {subscriber_id} registered channels={sorted(channel_set)} "
            f"priority={int(priority or 0)}"
        )
        try:
            while True:
                try:
                    event = q.get(timeout=SUBSCRIBE_HEARTBEAT_SECONDS)
                except queue.Empty:
                    # 心跳：上层收到 None 会检查客户端是否还在，断开则退出并释放线程
                    yield None
                    continue
                yield event
        finally:
            with self._lock:
                # 只在「自己仍是当前注册者」时移除订阅。
                # 关键：客户端重连时用同一个 subscriber_id 覆盖注册新订阅，
                # 旧 handler 随后退出；若无条件 pop，会把**新订阅**删掉，
                # 导致之后所有事件都投不出去（只能等下次重连时靠 buffer 回放），
                # 表现为"消息只在重连瞬间才到、平时收不到"。
                entry = self._subscribers.get(subscriber_id)
                if entry is not None and entry[1] is q:
                    self._subscribers.pop(subscriber_id, None)

    # ─── 发布 ───────────────────────────────────────────────

    def publish(self, event: dict):
        event_channel = str(event.get("channel") or "")
        suppressed = suppressed_channels(event)

        with self._lock:
            items = list(self._subscribers.items())

        # 事件显式屏蔽的渠道：跳过原始投递，直接走跨渠道转投（转投时也不会再选它）
        if event_channel and event_channel in suppressed:
            _log(
                f"channel={event_channel!r} suppressed by event metadata, "
                f"trying fallback {_brief(event)}"
            )
            self._fallback_chain(
                event,
                exclude_channel=event_channel,
                exclude_subscriber="",
                hops=1,
            )
            return

        # 第一优先：匹配原始 channel 的订阅者（同渠道多订阅者按优先级，**只投一个**）
        candidates = self._match_subscribers(items, event_channel)
        if candidates:
            needs_ack = is_ack_required(event)
            tried: list[str] = []
            for subscriber_id, q in candidates:
                to_send = event
                if needs_ack:
                    to_send = self._mark_ack(event)
                    self._register_inflight(
                        to_send, subscriber_id, hops=0, tried_subscribers=tried + [subscriber_id]
                    )
                try:
                    q.put_nowait(to_send)
                    _log(
                        f"deliver -> {subscriber_id} ack={needs_ack} "
                        f"(channel={event_channel!r} 候选 {len(candidates)} 个) {_brief(to_send)}"
                    )
                    return  # 已入队；投递结果等消费端 ack
                except queue.Full:
                    _log(f"queue full for {subscriber_id}, trying next {_brief(event)}")
                    if needs_ack:
                        with self._lock:
                            self._inflight.pop(str(to_send.get("event_id") or ""), None)
                    tried.append(subscriber_id)

        # 第二优先：跨渠道转投（无订阅者 / 入队失败）
        _log(f"no subscriber for channel={event_channel!r}, trying fallback {_brief(event)}")
        self._fallback_chain(
            event,
            exclude_channel=event_channel,
            exclude_subscriber="",
            hops=1,
        )

    # ─── 确认 ───────────────────────────────────────────────

    def ack(self, event_id: str, ok: bool, error: str = "") -> tuple[bool, str]:
        """消费端回报投递结果。返回 (accepted, disposition)。"""
        with self._lock:
            record = self._inflight.pop(str(event_id or ""), None)
        if record is None:
            # 未知/已处理（例如非 ack 事件、或已超时）：忽略
            _log(f"ack unknown event={event_id} ok={ok}")
            return False, "unknown"
        if ok:
            _log(f"ack ok event={event_id} sub={record.get('subscriber_id')}")
            return True, "acked"
        _log(
            f"ack FAILED event={event_id} sub={record.get('subscriber_id')} "
            f"reason={error or 'nack'} {_brief(record['event'])}"
        )
        return True, self._on_undelivered(record["event"], record, reason=error or "nack")

    def _on_undelivered(self, event: dict, record: dict, reason: str) -> str:
        # 1) 同渠道还能换一个订阅者（统一 qq 渠道后的主路径：
        #    llbot 优先级高但容易掉 → 失败后自动切到 qqofficial，仍是同一个 channel）
        event_channel = str(event.get("channel") or "")
        tried_subs = list(record.get("tried_subscribers") or [record.get("subscriber_id") or ""])
        with self._lock:
            items = list(self._subscribers.items())
        same_channel = self._match_subscribers(
            items,
            event_channel,
            exclude=str(record.get("subscriber_id") or ""),
            exclude_set={s for s in tried_subs if s},
        )
        for subscriber_id, q in same_channel:
            needs_ack = is_ack_required(event)
            to_send = event
            if needs_ack:
                to_send = self._mark_ack(event)
                self._register_inflight(
                    to_send,
                    subscriber_id,
                    hops=int(record.get("hops") or 0),
                    tried_subscribers=tried_subs + [subscriber_id],
                )
            try:
                q.put_nowait(to_send)
                _log(
                    f"switch backend -> {subscriber_id} channel={event_channel!r} "
                    f"reason={reason} {_brief(to_send)}"
                )
                return f"switched:{subscriber_id}"
            except queue.Full:
                if needs_ack:
                    with self._lock:
                        self._inflight.pop(str(to_send.get("event_id") or ""), None)

        # 2) 否则按原有跨渠道转投
        return self._fallback_chain(
            event,
            exclude_channel=event_channel,
            exclude_subscriber=str(record.get("subscriber_id") or ""),
            hops=int(record.get("hops") or 0) + 1,
            reason=reason,
        )

    # ─── 转投 / 缓冲 ────────────────────────────────────────

    def _fallback_chain(
        self,
        event: dict,
        *,
        exclude_channel: str,
        exclude_subscriber: str,
        hops: int,
        reason: str = "",
    ) -> str:
        user_id = str(event.get("user_id") or "")
        if not user_id:
            return "skipped"

        tried = {
            c for c in str(_meta(event).get(_META_TRIED_CHANNELS) or "").split(",") if c
        }
        tried.add(exclude_channel)

        if hops <= MAX_FALLBACK_HOPS:
            with self._lock:
                items = list(self._subscribers.items())
            suppressed = suppressed_channels(event)
            for channel, chan_uid, _prio in self._channel_targets(user_id):
                if channel in tried or (channel and channel in suppressed):
                    continue
                target = self._match_subscriber(items, channel, exclude=exclude_subscriber)
                if target is None:
                    continue
                subscriber_id, q = target
                forwarded = dict(event)
                forwarded["channel"] = channel
                # 按目标渠道身份改写 user_id：qq 事件转投 web 时必须换成 web 侧 id，
                # 否则网关按 h268 匹配 SSE 会落空（事件带的是 QQ 号）。
                forwarded["user_id"] = chan_uid or user_id
                # session_id 也必须按目标渠道重建（见 _channel_session_id）
                forwarded["session_id"] = _channel_session_id(
                    channel, chan_uid, str(event.get("session_id") or "")
                )
                forwarded["metadata"] = {
                    **_meta(event),
                    _META_FALLBACK_FROM: exclude_channel,
                    _META_FALLBACK_HOPS: str(hops),
                    _META_TRIED_CHANNELS: ",".join(sorted(c for c in tried if c)),
                }
                needs_ack = is_ack_required(forwarded)
                if needs_ack:
                    forwarded = self._mark_ack(forwarded)
                    self._register_inflight(forwarded, subscriber_id, hops=hops)
                try:
                    q.put_nowait(forwarded)
                    _log(
                        f"fallback -> {subscriber_id} channel={channel} hop={hops} "
                        f"user={forwarded['user_id']} session={forwarded['session_id']} "
                        f"id={forwarded.get('event_id')}"
                    )
                    return f"forwarded:{channel}"
                except queue.Full:
                    if needs_ack:
                        with self._lock:
                            self._inflight.pop(str(forwarded.get("event_id") or ""), None)

        stored = self._buffer_event(event)
        _log(
            f"no reachable channel (tried={sorted(c for c in tried if c)}) -> "
            f"{'buffered' if stored else 'dropped'} {_brief(event)}"
        )
        return "buffered" if stored else "dropped"

    def _buffer_event(self, event: dict) -> bool:
        """存入缓冲等渠道路径恢复。超过尝试上限则丢弃，避免"回放→失败→缓冲"死循环。"""
        user_id = str(event.get("user_id") or "")
        if not user_id:
            return False
        meta = dict(_meta(event))
        attempts = int(meta.get(_META_BUFFER_ATTEMPTS) or 0) + 1
        if attempts > MAX_BUFFER_ATTEMPTS:
            return False
        meta[_META_BUFFER_ATTEMPTS] = str(attempts)
        stored = dict(event)
        stored["metadata"] = meta
        with self._lock:
            buf = self._buffer.setdefault(user_id, [])
            if len(buf) >= MAX_BUFFER_PER_USER:
                buf.pop(0)
            buf.append(stored)
        return True

    def _requeue(self, user_id: str, event: dict) -> None:
        with self._lock:
            buf = self._buffer.setdefault(user_id, [])
            if len(buf) >= MAX_BUFFER_PER_USER:
                buf.pop(0)
            buf.append(event)

    # ─── 回放 ───────────────────────────────────────────────

    def _identity_closure(self, user_id: str) -> set[str]:
        """该用户的所有渠道身份（含自身）。

        别名解析让"渠道身份"与"规范 id"等价（`2195239690` ≡ `h268`），但缓冲的键是
        **最后一次尝试渠道的身份** —— 可能是其中任意一个。所以按 user_id flush 时
        必须做身份等价匹配，否则会漏掉"事件缓冲在另一个身份下"的消息。
        """
        ids = {user_id}
        for _chan, chan_uid, _prio in self._channel_targets(user_id):
            if chan_uid:
                ids.add(chan_uid)
        return ids

    def flush_buffered(self, user_id: str = "", channel: str = "") -> int:
        """某渠道重新可达时补发缓冲。

        web：SSE 连接建立时按 user_id 调；qq：Satori 重连后按 channel 调。
        期望渠道仍不可达的用户，其事件放回缓冲（不丢、也不强发）。
        """
        wanted = self._identity_closure(user_id) if user_id else set()
        with self._lock:
            if user_id:
                uids = [k for k in self._buffer if k in wanted]
            else:
                uids = list(self._buffer.keys())
            taken = {uid: self._buffer.pop(uid, []) for uid in uids if uid}
        _log(
            f"flush request user={user_id!r} channel={channel!r} "
            f"wanted={sorted(wanted)} keys={sorted(taken)} "
            f"buffered={ {k: len(v) for k, v in taken.items()} }"
        )

        flushed = 0
        for uid, events in taken.items():
            for event in events:
                pick = self._pick_reachable_target(uid, channel)
                if pick is None:
                    self._requeue(uid, event)
                    continue
                chan, chan_uid, subscriber_id, q = pick
                out = dict(event)
                out["channel"] = chan
                out["user_id"] = chan_uid or uid
                # session_id 同样按目标渠道重建（补发到 qq 时必须是 qq_{QQ号}）
                out["session_id"] = _channel_session_id(
                    chan, chan_uid, str(event.get("session_id") or "")
                )
                needs_ack = is_ack_required(out)
                if needs_ack:
                    out = self._mark_ack(out)
                    self._register_inflight(
                        out, subscriber_id,
                        hops=int(_meta(out).get(_META_FALLBACK_HOPS) or 0),
                    )
                try:
                    q.put_nowait(out)
                    flushed += 1
                    _log(
                        f"flush -> {subscriber_id} channel={chan} key={uid} "
                        f"user={out['user_id']} session={out['session_id']} "
                        f"id={out.get('event_id')}"
                    )
                except queue.Full:
                    if needs_ack:
                        with self._lock:
                            self._inflight.pop(str(out.get("event_id") or ""), None)
                    self._requeue(uid, out)
        return flushed

    def _pick_reachable_target(self, user_id: str, channel: str = ""):
        with self._lock:
            items = list(self._subscribers.items())
        for chan, chan_uid, _prio in self._channel_targets(user_id):
            if channel and chan != channel:
                continue
            target = self._match_subscriber(items, chan)
            if target is not None:
                return chan, chan_uid, target[0], target[1]
        return None

    # ─── inflight / 超时扫描 ────────────────────────────────

    @staticmethod
    def _mark_ack(event: dict) -> dict:
        marked = dict(event)
        marked["metadata"] = {**_meta(event), _META_ACK_REQUIRED: "1"}
        return marked

    def _register_inflight(
        self,
        event: dict,
        subscriber_id: str,
        hops: int,
        tried_subscribers: list[str] | None = None,
    ) -> None:
        event_id = str(event.get("event_id") or "")
        if not event_id:
            event_id = f"event-{uuid.uuid4().hex[:12]}"
            event["event_id"] = event_id
        with self._lock:
            self._inflight[event_id] = {
                "event": event,
                "subscriber_id": subscriber_id,
                "hops": hops,
                "deadline": time.time() + ACK_TIMEOUT_SECONDS,
                "tried_subscribers": list(tried_subscribers or [subscriber_id]),
            }

    def inflight_stats(self) -> dict:
        with self._lock:
            return {
                "inflight": len(self._inflight),
                "buffered_users": len([u for u, e in self._buffer.items() if e]),
                "buffered_total": sum(len(e) for e in self._buffer.values()),
            }

    def start_sweeper(self) -> None:
        """启动 ack 超时扫描线程（覆盖消费端崩溃/卡死：永远等不到 ack）。"""
        if self._sweeper_started:
            return
        self._sweeper_started = True
        thread = threading.Thread(target=self._sweep_loop, name="ack-sweeper", daemon=True)
        thread.start()

    def _sweep_loop(self) -> None:
        while True:
            time.sleep(ACK_SWEEP_INTERVAL_SECONDS)
            try:
                now = time.time()
                with self._lock:
                    expired = [
                        self._inflight.pop(eid)
                        for eid, rec in list(self._inflight.items())
                        if rec["deadline"] <= now
                    ]
                for record in expired:
                    self._on_undelivered(record["event"], record, reason="ack timeout")
            except Exception:
                # 扫描线程绝不能因单条事件异常而退出
                continue


event_bus = EventBus()


def make_event_id() -> str:
    return f"event-{uuid.uuid4().hex[:12]}"


def task_event(
    task: ScheduledTask,
    event_type: str,
    text: str = "",
    images: list[str] | None = None,
    waiting: int = 0,
    error: str = "",
    metadata: dict[str, str] | None = None,
) -> dict:
    meta = dict(metadata or {})
    # 定时任务（timer_task）的结果不允许落到受限渠道：
    # 官方 bot 的主动消息会被用户开关/频控拦下，走它等于丢消息。
    if str((task.metadata or {}).get("source") or "") == "timer_task":
        excluded = excluded_scheduled_channels()
        if excluded:
            merged = {
                item for item in str(meta.get(_META_SUPPRESS_CHANNELS) or "").split(",")
                if item
            }
            merged |= excluded
            meta[_META_SUPPRESS_CHANNELS] = ",".join(sorted(merged))
    return {
        "event_id": make_event_id(),
        "task_id": task.task_id,
        "user_id": task.user_id,
        "session_id": task.session_id,
        "channel": task.channel,
        "type": event_type,
        "text": text,
        "images": images or [],
        "waiting": waiting,
        "error": error,
        "delivery_target": task.delivery_target.to_dict(),
        "metadata": meta,
    }


def excluded_scheduled_channels() -> set[str]:
    """定时任务屏蔽的渠道（来自 scheduler config，缺省屏蔽 qqofficial）。"""
    try:
        from app import config as _config
        return set(getattr(_config, "SCHEDULED_EXCLUDED_CHANNELS", set()))
    except Exception:
        return set()
"""同渠道短窗口合并：把“转发 + 回复”这类成对到达的消息合成一个任务。

为什么在这里做：satori(1.3.2) 没有 call_api / message.get，官方渠道也没有合并转发节点的
拉取接口 —— 适配层拿不到转发内容，只能在**任务层**按到达时间合并。实测成对间隔：

    06:36:40.539 → 06:36:40.647   Δ 108 ms
    06:38:01.175 → 06:38:02.099   Δ 924 ms

所以滞留窗口默认 1200ms（覆盖 924ms 那组），到达时分三种情况：

1. 该用户空闲（会立刻开跑）→ 先滞留 hold_ms 再入队；滞留期间来的消息并入；
2. 该用户已有“排队未开跑”的任务 → 直接并入，无需滞留；
3. 该用户正在跑 → 不合并，走原有排队逻辑。

模块不依赖 gRPC/事件总线，便于单测。
"""

from __future__ import annotations

import threading
import time

MERGE_SEPARATOR = "\n\n【续】\n\n"


def _merge_header(task) -> str:
    """合并段的“前段”：`[MM-DD HH:MM] <user_id>:`（对齐平时消息的前缀格式）。

    时间戳取合并发生的时刻（本地时区），不需要额外查询；任一部分缺失就省略。
    """
    stamp = ""
    try:
        stamp = time.strftime("[%m-%d %H:%M] ")
    except Exception:  # noqa: BLE001 - 极端环境下 time 不可用则省略时间
        stamp = ""
    user_id = str(getattr(task, "user_id", "") or "").strip()
    if user_id:
        return f"{stamp}{user_id}:"
    return stamp.strip()

# 内容合并的标记：合并后 metadata 里带 `_mirrored_merge_parts` / `_merged_from`，
# 便于在日志和排障里看出“这条其实由几条合成”。


def has_images(task) -> bool:
    """任务是否带图片（适配层把 QQ 图片放进 images 列表）。"""
    return bool(getattr(task, "images", None))


def hold_window(task, hold_ms: int, hold_max_ms: int,
                image_hold_ms: int, image_hold_max_ms: int) -> tuple[int, int]:
    """按是否带图片选滞留窗口：带图更久（QQ 图片常分多条到达）。"""
    if has_images(task):
        return (max(int(image_hold_ms or 0), 0), max(int(image_hold_max_ms or 0), 0))
    return (max(int(hold_ms or 0), 0), max(int(hold_max_ms or 0), 0))


def merge_task(target, incoming, max_parts: int = 3) -> bool:
    """把 incoming 并入 target。超过段数上限返回 False（不合并）。"""
    try:
        parts = int(str((target.metadata or {}).get("_merge_parts") or "1"))
    except (TypeError, ValueError):
        parts = 1
    if parts >= max(1, int(max_parts)):
        return False

    header = _merge_header(incoming)
    separator = f"\n\n【续】{header}\n" if header else MERGE_SEPARATOR
    target.content = (target.content or "") + separator + (incoming.content or "")
    if getattr(target, "images", None) is not None:
        target.images = list(target.images or []) + list(getattr(incoming, "images", None) or [])

    meta = target.metadata if isinstance(target.metadata, dict) else {}
    meta["_merge_parts"] = str(parts + 1)
    merged = [item for item in str(meta.get("_merged_from") or "").split(",") if item]
    if incoming.task_id not in merged:
        merged.append(incoming.task_id)
    meta["_merged_from"] = ",".join(merged)
    target.metadata = meta
    return True


class HoldQueue:
    """滞留队列：短暂扣住第一条，等成对的第二条到达后合并再放行。

    `release(user_id, task)` 由调用方注入（scheduler 用它把任务真正入队）。
    """

    def __init__(self, release, logger=None):
        self._release = release
        self._log = logger or (lambda message: None)
        self._items: dict[str, dict] = {}
        self._lock = threading.Lock()

    # ── 查询 ─────────────────────────────────────────────
    def pending(self, user_id: str) -> bool:
        with self._lock:
            return user_id in self._items

    # ── 提交 ─────────────────────────────────────────────
    def hold(self, user_id: str, task, hold_ms: int, max_ms: int, max_parts: int) -> str:
        """滞留或合并。返回 'held' | 'merged'。"""
        now = time.monotonic()
        with self._lock:
            entry = self._items.get(user_id)
            if entry is not None:
                if merge_task(entry["task"], task, max_parts):
                    created = entry["created"]
                    deadline = min(created + max_ms / 1000.0, now + hold_ms / 1000.0)
                    entry["deadline"] = deadline
                    self._reschedule_locked(user_id, deadline - now)
                    self._log(
                        f"[merge] {user_id} 滞留中合并 {entry['task'].task_id} <- "
                        f"{task.task_id} (parts={entry['task'].metadata.get('_merge_parts')})"
                    )
                    return "merged"
                # 段数到顶：把滞留中的先放行，新任务自立门户（不丢任何内容）
                self._flush_locked(user_id)

            deadline = now + hold_ms / 1000.0
            entry = {
                "task": task,
                "created": now,
                "deadline": deadline,
                "timer": None,
            }
            self._items[user_id] = entry
            self._reschedule_locked(user_id, hold_ms / 1000.0)
            self._log(f"[merge] {user_id} 滞留 {task.task_id}（{hold_ms}ms 等成对消息）")
            return "held"

    # ── 释放 ─────────────────────────────────────────────
    def flush(self, user_id: str) -> None:
        with self._lock:
            self._flush_locked(user_id)

    def _reschedule_locked(self, user_id: str, delay: float) -> None:
        entry = self._items.get(user_id)
        if entry is None:
            return
        timer = entry.get("timer")
        if timer is not None:
            timer.cancel()
        delay = max(delay, 0.01)
        timer = threading.Timer(delay, self._on_timer, args=(user_id,))
        timer.daemon = True
        entry["timer"] = timer
        timer.start()

    def _on_timer(self, user_id: str) -> None:
        with self._lock:
            entry = self._items.get(user_id)
            if entry is None:
                return
            remaining = entry["deadline"] - time.monotonic()
            if remaining > 0.01:
                # 期间被延长过：按新的 deadline 再来一次
                self._reschedule_locked(user_id, remaining)
                return
            task = entry["task"]
            del self._items[user_id]
        try:
            self._release(user_id, task)
        except Exception as exc:  # noqa: BLE001 - 释放失败不能吞掉任务
            self._log(f"[merge] {user_id} 释放失败: {exc}")

    def _flush_locked(self, user_id: str) -> None:
        """把滞留项立刻交给 release（持锁内部使用，调用完会自动清理）。"""
        entry = self._items.pop(user_id, None)
        if entry is None:
            return
        timer = entry.get("timer")
        if timer is not None:
            timer.cancel()
        task = entry["task"]
        try:
            self._release(user_id, task)
        except Exception as exc:  # noqa: BLE001
            self._log(f"[merge] {user_id} 强制释放失败: {exc}")

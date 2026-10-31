"""qq-onebot-service 入口（OneBot 11：NapCat / LLOneBot / go-cqhttp 都能连）。

流程与 satori 那条（qq-llbot-service）保持一致：
  QQ 消息 → CreateTask → scheduler → orchestrator → 事件回流 → 推送 QQ

差异只在适配层：这里用 OneBot 11 的 WebSocket（正向 / 反向），
订阅优先级默认 90（satori 100 优先、官方 50 兜底）。
"""

from __future__ import annotations

import asyncio
import re

from app import config
from app.onebot_bridge import OneBotBridge
from app.scheduler_client import SchedulerClient


def _safe_workspace_segment(user_id: str) -> str:
    raw = str(user_id or "default").strip() or "default"
    safe = re.sub(r"[^0-9A-Za-z_.@-]+", "_", raw).strip("._") or "default"
    return "default" if safe in {".", ".."} else safe


async def _save_files(user_id: str, files: list[dict]) -> list[str]:
    """把收到的文件落到用户工作区（与 orchestrator/tool-runtime 同一路径规则）。"""
    notes: list[str] = []
    if not files:
        return notes
    safe_user = _safe_workspace_segment(user_id)
    user_dir = config.WORKSPACE_DIR / "users" / safe_user
    try:
        user_dir.mkdir(parents=True, exist_ok=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[qq-onebot] 无法创建用户工作区 {user_dir}: {exc}", flush=True)
        return notes

    for item in files:
        name = str(item.get("name") or "file")
        name = re.sub(r"[^0-9A-Za-z_.\-\u4e00-\u9fff]+", "_", name)[:80] or "file"
        url = str(item.get("url") or "")
        if not url:
            notes.append(f"[收到文件] {name}（无下载地址，OneBot 实现未提供 url）")
            continue
        try:
            import httpx

            async with httpx.AsyncClient(timeout=config.ONEBOT_IMAGE_TIMEOUT) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                dest = user_dir / name
                dest.write_bytes(resp.content)
            rel = f"/app/workspace/users/{safe_user}/{name}"
            notes.append(f"[已收到文件] {name}（{len(resp.content)} bytes）已保存至 {rel}，可用文件工具读取")
        except Exception as exc:  # noqa: BLE001
            notes.append(f"[收到文件] {name}，但下载失败：{exc}")
    return notes


async def main() -> None:
    bridge = OneBotBridge()
    scheduler = SchedulerClient(config.SCHEDULER_TARGET)

    # ── QQ 消息 → scheduler ────────────────────────────────
    async def on_qq_message(parsed) -> None:
        content = parsed.text
        images = parsed.images[: config.ONEBOT_MAX_IMAGES]
        if not content and images:
            content = "[图片]"

        file_notes = await _save_files(parsed.user_id, parsed.files)
        if file_notes:
            content = (content + "\n" if content else "") + "\n".join(file_notes)
        if not content and not images:
            return

        result = await scheduler.create_task(
            user_id=parsed.user_id,
            content=content,
            channel="qq",
            session_id=parsed.session_id,
            images=images,
        )
        if result["ok"]:
            print(
                f"[qq-onebot] 任务已提交 user={parsed.user_id} session={parsed.session_id} "
                f"task_id={result['task_id']}",
                flush=True,
            )
        else:
            await bridge.send(parsed.session_id, f"提交失败：{result['error']}")

    bridge.on_qq_message(on_qq_message)

    # ── 上下线边沿 ────────────────────────────────────────
    online_event = asyncio.Event()
    online_event.set()

    async def on_qq_online() -> None:
        online_event.set()
        flushed = await scheduler.flush_buffered(channel="qq")
        if flushed:
            print(f"[qq-onebot] 已请求补发缓冲消息 {flushed} 条", flush=True)

    async def on_qq_offline() -> None:
        if online_event.is_set():
            print("[qq-onebot] QQ 掉线：退订调度器事件（让另一个后端接手）", flush=True)
        online_event.clear()

    bridge.setup_lifecycle(on_qq_online, on_qq_offline)

    # ── scheduler 事件 → QQ 推送 ──────────────────────────
    async def event_loop() -> None:
        fail_streak = 0
        while True:
            if not online_event.is_set():
                print("[qq-onebot] QQ 离线中：已退订调度器事件，等待上线边沿", flush=True)
                await online_event.wait()
                print("[qq-onebot] QQ 已恢复：重新订阅调度器事件", flush=True)
            try:
                async for event in scheduler.subscribe_events(
                    subscriber_id=config.SUBSCRIBER_ID,
                    channels=["qq"],
                    priority=config.SUBSCRIBER_PRIORITY,
                ):
                    event_type = event.get("type", "")
                    event_id = event.get("event_id", "")
                    user_id = event.get("user_id", "")
                    session_id = event.get("session_id", "") or f"qq_{user_id}"
                    text = event.get("text", "")
                    images = event.get("images", [])
                    metadata = event.get("metadata", {})
                    needs_ack = str(metadata.get("_ack_required") or "").strip() == "1"

                    delivered = None
                    if event_type in {"assistant_message", "assistant_intermediate"} and metadata.get("visible_to_user") == "true":
                        delivered = await bridge.send(session_id, text=text, images=images)
                    elif event_type == "task_waiting_user":
                        delivered = await bridge.send(
                            session_id, text=text or "出现某些问题，询问无法发送，请重新发送", images=[]
                        )
                    elif event_type in {
                        "task_failed",
                        "task_timeout",
                        "task_cancelled",
                        "task_error",
                        "task_finished_with_error",
                    }:
                        delivered = await bridge.send(
                            session_id, text=f"任务失败：{text or event.get('error') or '未知错误'}", images=[]
                        )

                    if needs_ack and delivered is not None:
                        disposition = await scheduler.ack_event(
                            subscriber_id=config.SUBSCRIBER_ID,
                            event_id=event_id,
                            ok=bool(delivered),
                            error="" if delivered else (
                                "onebot offline" if not bridge.is_qq_online() else "send failed"
                            ),
                        )
                        if not delivered:
                            print(
                                f"[qq-onebot] 投递失败已回报 event={event_id} type={event_type} "
                                f"disposition={disposition}",
                                flush=True,
                            )
                            fail_streak += 1
                            if fail_streak >= int(config.OFFLINE_SEND_FAILURES or 3):
                                fail_streak = 0
                                await on_qq_offline()
                                break
                        else:
                            fail_streak = 0

                    if not online_event.is_set():
                        break
            except Exception as exc:  # noqa: BLE001
                print(f"[qq-onebot] 事件订阅中断：{exc}（2s 后重订）", flush=True)
                await asyncio.sleep(2)
                continue

    print(
        f"[qq-onebot] starting... mode={config.ONEBOT_MODE} "
        f"url={config.ONEBOT_WS_URL} listen={config.ONEBOT_WS_LISTEN} "
        f"scheduler={config.SCHEDULER_TARGET} subscriber={config.SUBSCRIBER_ID}",
        flush=True,
    )
    await asyncio.gather(bridge.run(), event_loop())


if __name__ == "__main__":
    asyncio.run(main())

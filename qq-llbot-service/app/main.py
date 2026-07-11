"""
qq-llbot-service 入口。

启动流程：
1. 初始化 Satori QQ 桥接
2. 连接 task-scheduler-service 订阅事件流
3. QQ 消息 → CreateTask → scheduler → orchestrator → 事件回流 → 推送 QQ
"""
import asyncio

from app import config
from app.qq_bridge import QQBridge
from app.scheduler_client import SchedulerClient


async def main():
    bridge = QQBridge()
    scheduler = SchedulerClient(config.SCHEDULER_TARGET)

    # ── QQ 消息 ─→ scheduler ──────────────────────────────
    async def on_qq_message(user_id: str, content: str):
        result = await scheduler.create_task(
            user_id=user_id,
            content=content,
            channel="qq",
        )
        if result["ok"]:
            print(
                f"[qq-llbot] 任务已提交 user={user_id} task_id={result['task_id']}",
                flush=True,
            )
        else:
            await bridge.send(user_id, f"提交失败：{result['error']}")

    bridge.on_qq_message(on_qq_message)
    bridge.setup_listener()

    # ── scheduler 事件 → QQ 推送 ──────────────────────────
    async def event_loop():
        async for event in scheduler.subscribe_events(
            subscriber_id=config.SUBSCRIBER_ID,
            channels=["qq"],
        ):
            event_type = event.get("type", "")
            user_id = event.get("user_id", "")
            text = event.get("text", "")
            images = event.get("images", [])
            metadata = event.get("metadata", {})

            # 只推送用户可见的 assistant_message
            if event_type == "assistant_message" and metadata.get("visible_to_user") == "true":
                await bridge.send(user_id, text=text, images=images)

    # ── 并行启动 ──────────────────────────────────────────
    await asyncio.gather(
        bridge.run(),
        event_loop(),
    )


if __name__ == "__main__":
    print(f"[qq-llbot] starting... scheduler={config.SCHEDULER_TARGET} satori={config.SATORI_HOST}:{config.SATORI_PORT}", flush=True)
    asyncio.run(main())

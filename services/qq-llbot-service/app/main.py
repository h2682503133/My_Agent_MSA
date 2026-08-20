"""
qq-llbot-service 入口。

启动流程：
1. 初始化 Satori QQ 桥接
2. 连接 task-scheduler-service 订阅事件流
3. QQ 消息 → CreateTask → scheduler → orchestrator → 事件回流 → 推送 QQ

文件收发：
- 收：QQ 消息中的 File 元素下载后保存到用户工作空间
      /app/workspace/users/<user_id>/，并在 content 中附路径说明，
      agent 可用 file-read 等工具读取。
- 发：保持现有 image 方式（agent 工具产出 asset_url → assistant_message 的
      images 字段 → QQ 以 Image 元素推送），暂不引入独立文件事件。
"""
import asyncio
import re

from app import config
from app.qq_bridge import QQBridge
from app.scheduler_client import SchedulerClient


def _safe_workspace_segment(user_id: str) -> str:
    """把 user_id 转成安全目录名，与 orchestrator/tool-runtime 的规则保持一致。"""
    raw = str(user_id or "default").strip()
    if not raw:
        raw = "default"
    safe = re.sub(r"[^0-9A-Za-z_.@-]+", "_", raw)
    safe = safe.strip("._") or "default"
    if safe in {".", ".."}:
        safe = "default"
    return safe


async def _save_files(user_id: str, files: list[tuple[str, bytes]]) -> list[str]:
    """把收到的文件写入用户工作空间，返回给 agent 的路径说明列表。

    文件名为 qq_bridge 清洗后的 basename，路径统一为
    /app/workspace/users/<safe_user_id>/<name>（与 tool-runtime 一致）。
    """
    notes: list[str] = []
    if not files:
        return notes
    safe_user = _safe_workspace_segment(user_id)
    user_dir = config.WORKSPACE_DIR / "users" / safe_user
    try:
        user_dir.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        print(f"[qq-llbot] 无法创建用户工作空间 {user_dir}: {exc}", flush=True)
        return notes

    for name, data in files:
        try:
            dest = user_dir / name
            dest.write_bytes(data)
            rel = f"/app/workspace/users/{safe_user}/{name}"
            notes.append(f"[已收到文件] {name}（{len(data)} bytes）已保存至 {rel}，可用文件工具读取")
            print(f"[qq-llbot] 已保存文件 {dest} ({len(data)} bytes)", flush=True)
        except Exception as exc:
            print(f"[qq-llbot] 保存文件失败 {name}: {exc}", flush=True)
    return notes


async def main():
    bridge = QQBridge()
    scheduler = SchedulerClient(config.SCHEDULER_TARGET)

    # ── QQ 消息 ─→ scheduler ──────────────────────────────
    async def on_qq_message(
        user_id: str,
        content: str,
        session_id: str,
        image_urls: list[str],
        files: list[tuple[str, bytes]],
    ):
        images = [img for img in await bridge.resolve_images(image_urls) if img]
        if not content and images:
            content = "[图片]"

        # 收到的文件保存到用户工作空间，并在 content 中告知 agent 路径
        file_notes = await _save_files(user_id, files)
        if file_notes:
            content = (content + "\n" if content else "") + "\n".join(file_notes)
        if not content and not images:
            return

        result = await scheduler.create_task(
            user_id=user_id,
            content=content,
            channel="qq",
            session_id=session_id,
            images=images,
        )
        if result["ok"]:
            print(
                f"[qq-llbot] 任务已提交 user={user_id} session={session_id} "
                f"task_id={result['task_id']}",
                flush=True,
            )
        else:
            await bridge.send(session_id, f"提交失败：{result['error']}")

    bridge.on_qq_message(on_qq_message)
    bridge.setup_listener()

    # ── scheduler 事件 → QQ 推送 ──────────────────────────
    async def event_loop():
        async for event in scheduler.subscribe_events(
            subscriber_id=config.SUBSCRIBER_ID,
            channels=["qq"],
        ):
            event_type = event.get("type", "")
            # 群聊/私聊都按会话路由，私聊时 session_id 即 qq_{user_id}
            user_id = event.get("user_id", "")
            session_id = event.get("session_id", "") or f"qq_{user_id}"
            text = event.get("text", "")
            images = event.get("images", [])
            metadata = event.get("metadata", {})

            # 推送用户可见的 assistant_message / assistant_intermediate
            # （intermediate 含解析指令时提前转发给用户的说明文本）
            if event_type in {"assistant_message", "assistant_intermediate"} and metadata.get("visible_to_user") == "true":
                await bridge.send(session_id, text=text, images=images)
            # 询问挂起：把「询问：xxx」推给用户等待回复
            elif event_type == "task_waiting_user":
                await bridge.send(session_id, text=text or "出现某些问题，询问无法发送，请重新发送", images=[])
            # 任务失败/超时/取消等终态也要提示用户，避免静默无响应
            elif event_type in {
                "task_failed",
                "task_timeout",
                "task_cancelled",
                "task_error",
                "task_finished_with_error",
            }:
                message = text or event.get("error") or "任务失败"
                await bridge.send(session_id, text=f"任务失败：{message}", images=[])

    # ── 并行启动 ──────────────────────────────────────────
    await asyncio.gather(
        bridge.run(),
        event_loop(),
    )


if __name__ == "__main__":
    print(f"[qq-llbot] starting... scheduler={config.SCHEDULER_TARGET} satori={config.SATORI_HOST}:{config.SATORI_PORT}", flush=True)
    asyncio.run(main())

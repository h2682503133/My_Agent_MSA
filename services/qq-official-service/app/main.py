"""qq-official-service 入口：WebSocket 收官方事件 → 调度器；调度器事件 → 官方接口。

与 qq-llbot-service 的框架契约完全一致，只有两点不同：
- 渠道名 qqofficial（路由用，避免和 llbot 的 qq 抢同一条事件）
- 传输层是官方 WS 网关 + OpenAPI，而不是 Satori
"""
import asyncio

from . import config
from .bridge import OfficialBridge
from .openapi_client import OpenApiClient
from .scheduler_client import SchedulerClient
from .summarizer import ReaderSummarizer
from .token_store import TokenStore
from .user_client import UserClient
from .ws_client import GatewayClient

VISIBLE_EVENTS = {"assistant_message", "assistant_intermediate"}
FAILURE_EVENTS = {
    "task_failed",
    "task_timeout",
    "task_cancelled",
    "task_error",
    "task_finished_with_error",
}


async def _token_refresh_loop(tokens: TokenStore):
    while True:
        await asyncio.sleep(30 * 60)
        try:
            await tokens.get(force=True)
        except Exception as exc:
            print(f"[qq-official] token 定期刷新失败: {exc}", flush=True)


async def main():
    if not config.is_configured():
        raise RuntimeError(
            "缺少 app_id / client_secret：请在配置文件或环境变量里提供 "
            "QQ_OFFICIAL_APP_ID / QQ_OFFICIAL_CLIENT_SECRET"
        )

    tokens = TokenStore(config.API_BASE, config.APP_ID, config.CLIENT_SECRET)
    openapi = OpenApiClient(config.API_BASE, tokens)
    users = UserClient(config.USER_SERVICE_TARGET, config.CHANNEL)
    scheduler = SchedulerClient(config.SCHEDULER_TARGET)
    summarizer = ReaderSummarizer(
        config.MODEL_PROXY_TARGET,
        config.READER_PROFILE,
        config.SUMMARY_TIMEOUT_SECONDS,
    )
    bridge = OfficialBridge(scheduler, users, openapi, summarizer=summarizer)

    await tokens.get()
    if not tokens.token:
        # 明确失败，而不是带着无效凭据空转（dashboard 保存后重启能看到这条）
        raise RuntimeError(
            f"access_token 获取失败：检查 AppID/AppSecret（app_id={config.APP_ID}）。"
            "getAppAccessToken 失败原因见上方日志（常见 100007 AppID 无效 / 100016 secret 错误）"
        )

    async def on_ready():
        # 网关就绪 → 让调度器补发它缓冲的用户可见消息
        flushed = await scheduler.flush_buffered(channel=config.CHANNEL)
        if flushed:
            print(f"[qq-official] 已请求补发缓冲消息 {flushed} 条", flush=True)

    gateway = GatewayClient(
        gateway_getter=openapi.get_gateway,
        token_getter=tokens.get,
        intents=config.INTENTS,
        on_dispatch=bridge.handle_dispatch,
        on_ready=on_ready,
    )

    async def event_loop():
        async for event in scheduler.subscribe_events(
            subscriber_id=config.SUBSCRIBER_ID,
            channels=[config.CHANNEL],
        ):
            event_type = str(event.get("type") or "")
            event_id = str(event.get("event_id") or "")
            user_id = str(event.get("user_id") or "")
            session_id = str(event.get("session_id") or "") or f"qq_{user_id}"
            text = str(event.get("text") or "")
            images = list(event.get("images") or [])
            metadata = event.get("metadata") or {}
            needs_ack = str(metadata.get("_ack_required") or "").strip() == "1"

            delivered = None
            if event_type in VISIBLE_EVENTS and metadata.get("visible_to_user") == "true":
                # assistant_intermediate 是中间消息（受额度控制），assistant_message 是最终回复
                delivered = await bridge.deliver(
                    session_id,
                    text,
                    images,
                    final=event_type == "assistant_message",
                )
            elif event_type == "task_waiting_user":
                delivered = await bridge.deliver(
                    session_id,
                    text or "出现某些问题，询问无法发送，请重新发送",
                    [],
                    final=True,
                )
            elif event_type in FAILURE_EVENTS:
                message = text or event.get("error") or "任务失败"
                delivered = await bridge.deliver(session_id, f"任务失败：{message}", [], final=True)

            if needs_ack and delivered is not None:
                disposition = await scheduler.ack_event(
                    subscriber_id=config.SUBSCRIBER_ID,
                    event_id=event_id,
                    ok=bool(delivered),
                    error="" if delivered else "qq official send failed",
                )
                if not delivered:
                    print(
                        f"[qq-official] 投递失败已回报 event={event_id} "
                        f"type={event_type} disposition={disposition}",
                        flush=True,
                    )

    print(
        f"[qq-official] starting... app_id={config.APP_ID} "
        f"channel={config.CHANNEL} scheduler={config.SCHEDULER_TARGET}",
        flush=True,
    )
    await asyncio.gather(
        gateway.run(),
        event_loop(),
        _token_refresh_loop(tokens),
    )


if __name__ == "__main__":
    asyncio.run(main())

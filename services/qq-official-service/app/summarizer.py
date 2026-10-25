"""中间消息摘要：用 orchestrator 同一套 reader 模型档案做压缩。

为什么不用本地截断：中间消息合并后可能超长，超长会触发折半重发，
把宝贵的被动额度多花一次；所以优先让 `reader` 智能体压成一条短消息。

调用方式与 agent-orchestrator 的 `ModelProxyClient.chat_completion` 一致：
gRPC → model-proxy-service，`model_profile="reader"`（对应 model_list.json 的 reader 档案）。

任何失败（超时、模型不可用、空结果）都返回空串，由调用方退化为头尾截断——
摘要只是优化，不能阻塞投递。
"""
import asyncio
import sys
import uuid
from pathlib import Path

import grpc

PROTO_GEN_DIR = Path(__file__).parent.parent / "proto_gen"
if str(PROTO_GEN_DIR) not in sys.path:
    sys.path.insert(0, str(PROTO_GEN_DIR))

try:
    import model_proxy_pb2
    import model_proxy_pb2_grpc
except ImportError:  # pragma: no cover - 构建期错误
    model_proxy_pb2 = None
    model_proxy_pb2_grpc = None


SUMMARY_SYSTEM_PROMPT = (
    "你要把一段「智能体执行过程的中间消息」压缩成一条简短进展说明，"
    "让用户知道当前做到哪一步。要求：只输出压缩后的正文；"
    "保留关键结论、数量、路径、错误等事实；不要寒暄，不要 markdown 标题；"
    "长度不超过 {limit} 字。"
)


class ReaderSummarizer:
    def __init__(self, target: str, profile: str = "reader", timeout_seconds: int = 20):
        self.target = target
        self.profile = profile
        self.timeout = timeout_seconds

    async def summarize(self, text: str, limit: int) -> str:
        """返回不超过 limit 字的摘要；失败返回空串（由调用方兜底）。"""
        if model_proxy_pb2 is None:
            return ""
        text = (text or "").strip()
        if not text:
            return ""
        limit = max(int(limit or 200), 80)
        try:
            request = model_proxy_pb2.ChatCompletionRequest(
                request_id=f"qq-official-summary-{uuid.uuid4().hex[:12]}",
                agent_id="reader",
                model_profile=self.profile,
                messages=[
                    model_proxy_pb2.Message(
                        role="system",
                        content=SUMMARY_SYSTEM_PROMPT.format(limit=limit),
                    ),
                    model_proxy_pb2.Message(role="user", content=text),
                ],
                params={
                    "temperature": "0.3",
                    "max_tokens": str(min(max(limit * 2, 256), 1024)),
                    "stream": "false",
                },
            )
            async with grpc.aio.insecure_channel(self.target) as chan:
                stub = model_proxy_pb2_grpc.ModelProxyStub(chan)
                resp = await asyncio.wait_for(
                    stub.ChatCompletion(request, timeout=self.timeout),
                    timeout=self.timeout + 2,
                )
            if not resp.ok:
                print(f"[qq-official] reader 摘要失败: {resp.error}", flush=True)
                return ""
            summary = (resp.text or "").strip()
            if not summary:
                return ""
            return summary[:limit]
        except Exception as exc:
            print(f"[qq-official] reader 摘要异常: {type(exc).__name__} {exc}", flush=True)
            return ""

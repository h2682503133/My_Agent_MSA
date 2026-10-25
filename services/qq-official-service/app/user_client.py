"""user-service gRPC 客户端：解析 openid ↔ 规范 user_id，读取渠道地址。

身份规则（本项目约定）：
- 官方 bot 的 user_openid 按 AppID 隔离，拿不到 QQ 号；
- 绑定动作 = user-service.BindChannel(user_id=规范id, channel="qqofficial",
  channel_user_id=openid)，user-service 会写别名指针，之后 GetUser(openid)
  直接返回**规范记录**（透明跟随别名）；
- 因此入站只要 GetUser(openid) 就能拿到规范 user_id，未绑定时退回 openid。
"""
import json
import sys
from pathlib import Path

import grpc

PROTO_GEN_DIR = Path(__file__).parent.parent / "proto_gen"
if str(PROTO_GEN_DIR) not in sys.path:
    sys.path.insert(0, str(PROTO_GEN_DIR))

try:
    import user_pb2
    import user_pb2_grpc
except ImportError as exc:  # pragma: no cover - 构建期错误
    raise RuntimeError(
        "user gRPC generated files not found. Run bash scripts/gen_proto.sh first."
    ) from exc


class UserClient:
    def __init__(self, target: str, channel: str):
        self.target = target
        self.channel = channel

    async def get_user(self, user_id: str) -> dict:
        """读用户（跟随别名）。失败返回 {}，调用方按未绑定处理。"""
        try:
            async with grpc.aio.insecure_channel(self.target) as chan:
                stub = user_pb2_grpc.UserStub(chan)
                resp = await stub.GetUser(
                    user_pb2.GetUserRequest(user_id=user_id), timeout=3
                )
                if not resp.ok:
                    return {}
                data = json.loads(resp.user_json or "{}")
                return data if isinstance(data, dict) else {}
        except Exception as exc:
            print(f"[qq-official] user-service 查询失败 user={user_id}: {exc}", flush=True)
            return {}

    async def resolve_canonical(self, openid: str) -> str:
        """openid → 规范 user_id；未绑定则原样返回 openid。"""
        data = await self.get_user(openid)
        canonical = str(data.get("user_id") or "").strip()
        return canonical or openid

    async def channel_user_id(self, user_id: str, channel: str = "") -> str:
        """规范 user_id → 该渠道的地址（官方 bot 场景即 openid）。"""
        chan = channel or self.channel
        data = await self.get_user(user_id)
        channels = data.get("channels") if isinstance(data.get("channels"), dict) else {}
        entry = channels.get(chan) if isinstance(channels, dict) else None
        if isinstance(entry, dict):
            return str(entry.get("channel_user_id") or "").strip()
        return ""

    async def bind(self, user_id: str, channel_user_id: str, priority: int = 0) -> tuple[bool, str]:
        """把渠道身份绑定到规范用户（供运维/绑定命令调用）。"""
        try:
            async with grpc.aio.insecure_channel(self.target) as chan:
                stub = user_pb2_grpc.UserStub(chan)
                resp = await stub.BindChannel(
                    user_pb2.BindChannelRequest(
                        user_id=user_id,
                        channel=self.channel,
                        channel_user_id=channel_user_id,
                        priority=priority,
                    ),
                    timeout=5,
                )
                return bool(resp.ok), resp.message
        except Exception as exc:
            return False, str(exc)

"""user-service gRPC 代理客户端。"""

import os
import sys
from pathlib import Path

import grpc

GENERATED_DIR = Path(__file__).parent / "proto_gen"
if str(GENERATED_DIR) not in sys.path:
    sys.path.insert(0, str(GENERATED_DIR))

try:
    import user_pb2
    import user_pb2_grpc
except ImportError:
    user_pb2 = None
    user_pb2_grpc = None


class UserClient:
    def __init__(self, target: str | None = None):
        self.target = target or os.getenv(
            "USER_GRPC_TARGET",
            "user-service.agent.svc.cluster.local:5301",
        )

    def _ensure_imports(self):
        if user_pb2 is None or user_pb2_grpc is None:
            raise RuntimeError(
                "user gRPC stubs not generated. "
                "Run generate_proto.sh with user.proto in proto/."
            )

    async def get_user(self, user_id: str) -> dict:
        self._ensure_imports()
        try:
            async with grpc.aio.insecure_channel(self.target) as channel:
                stub = user_pb2_grpc.UserStub(channel)
                req = user_pb2.GetUserRequest(user_id=user_id)
                resp = await stub.GetUser(req)
                return {
                    "ok": resp.ok,
                    "user_id": resp.user_id,
                    "user_json": resp.user_json,
                }
        except Exception as exc:
            return {"ok": False, "user_id": user_id, "error": str(exc)}

    async def upsert_user(self, user_id: str, user_json: str) -> dict:
        self._ensure_imports()
        try:
            async with grpc.aio.insecure_channel(self.target) as channel:
                stub = user_pb2_grpc.UserStub(channel)
                req = user_pb2.UpsertUserRequest(
                    user_id=user_id,
                    user_json=user_json,
                )
                resp = await stub.UpsertUser(req)
                return {"ok": resp.ok, "user_id": resp.user_id}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    async def bind_channel(
        self,
        user_id: str,
        channel: str,
        channel_user_id: str,
        priority: int = 0,
    ) -> dict:
        self._ensure_imports()
        try:
            async with grpc.aio.insecure_channel(self.target) as channel:
                stub = user_pb2_grpc.UserStub(channel)
                req = user_pb2.BindChannelRequest(
                    user_id=user_id,
                    channel=channel,
                    channel_user_id=channel_user_id,
                    priority=priority,
                )
                resp = await stub.BindChannel(req)
                return {"ok": resp.ok, "message": resp.message}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    async def unbind_channel(self, user_id: str, channel: str) -> dict:
        self._ensure_imports()
        try:
            async with grpc.aio.insecure_channel(self.target) as channel:
                stub = user_pb2_grpc.UserStub(channel)
                req = user_pb2.UnbindChannelRequest(
                    user_id=user_id,
                    channel=channel,
                )
                resp = await stub.UnbindChannel(req)
                return {"ok": resp.ok, "message": resp.message}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}


def build_user_client() -> UserClient:
    return UserClient()

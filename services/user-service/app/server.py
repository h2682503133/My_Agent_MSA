import json
import sys
from concurrent import futures
from pathlib import Path

import grpc

from app import config
from app.user_store import user_store

GENERATED_DIR = Path(__file__).parent / "generated"
if str(GENERATED_DIR) not in sys.path:
    sys.path.insert(0, str(GENERATED_DIR))

try:
    import user_pb2
    import user_pb2_grpc
except ImportError as e:
    raise RuntimeError(
        "gRPC generated files not found. Run bash scripts/gen_proto.sh first."
    ) from e


class UserService(user_pb2_grpc.UserServicer):
    def GetUser(self, request, context):
        data = user_store.get_user(request.user_id)
        if data is None:
            return user_pb2.GetUserResponse(ok=False, user_id=request.user_id, user_json="")
        return user_pb2.GetUserResponse(
            ok=True,
            user_id=request.user_id,
            user_json=json.dumps(data, ensure_ascii=False),
        )

    def UpsertUser(self, request, context):
        result = user_store.upsert_user(request.user_id, request.user_json)
        ok = result != "JSON 格式错误"
        return user_pb2.UpsertUserResponse(ok=ok, user_id=result)

    def DeleteUser(self, request, context):
        ok = user_store.delete_user(request.user_id)
        return user_pb2.DeleteUserResponse(
            ok=ok,
            message="已删除" if ok else "用户不存在",
        )

    def ListUsers(self, request, context):
        users = user_store.list_users()
        return user_pb2.ListUsersResponse(
            users=[
                user_pb2.UserSummary(
                    user_id=u["user_id"],
                    created_at=u.get("created_at", ""),
                )
                for u in users
            ]
        )

    def BindChannel(self, request, context):
        result = user_store.bind_channel(
            request.user_id,
            request.channel,
            request.channel_user_id,
            request.priority,
        )
        return user_pb2.BindChannelResponse(
            ok=result == "ok",
            message=result,
        )

    def UnbindChannel(self, request, context):
        result = user_store.unbind_channel(request.user_id, request.channel)
        return user_pb2.UnbindChannelResponse(
            ok=result == "ok",
            message=result,
        )

    def SetOpenVikingKey(self, request, context):
        ok = user_store.set_openviking_key(request.user_id, request.api_key)
        return user_pb2.SetOpenVikingKeyResponse(ok=ok)

    def GetOpenVikingKey(self, request, context):
        key = user_store.get_openviking_key(request.user_id)
        return user_pb2.GetOpenVikingKeyResponse(
            ok=key is not None,
            api_key=key or "",
        )


def serve():
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=16))
    user_pb2_grpc.add_UserServicer_to_server(UserService(), server)
    listen_addr = f"{config.USER_GRPC_HOST}:{config.USER_GRPC_PORT}"
    server.add_insecure_port(listen_addr)
    server.start()
    print(f"user-service started on {listen_addr}", flush=True)
    server.wait_for_termination()

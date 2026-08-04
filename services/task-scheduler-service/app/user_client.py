"""user-service gRPC 客户端，供 scheduler 查询用户绑定渠道。"""
import sys
from pathlib import Path

import grpc

GENERATED_DIR = Path(__file__).parent / "generated"
if str(GENERATED_DIR) not in sys.path:
    sys.path.insert(0, str(GENERATED_DIR))

try:
    import user_pb2
    import user_pb2_grpc
except ImportError:
    user_pb2 = None
    user_pb2_grpc = None


class UserClient:
    def __init__(self, target: str = "user-service.agent.svc.cluster.local:5104"):
        self.target = target

    def _ensure_imports(self):
        if user_pb2 is None or user_pb2_grpc is None:
            raise RuntimeError("user gRPC stubs not generated. Run gen_proto.sh first.")

    def get_channels(self, user_id: str) -> list[str]:
        """返回用户绑定的所有渠道名列表，失败返回空列表。"""
        self._ensure_imports()
        import json
        try:
            with grpc.insecure_channel(self.target) as channel:
                stub = user_pb2_grpc.UserStub(channel)
                req = user_pb2.GetUserRequest(user_id=user_id)
                resp = stub.GetUser(req, timeout=3)
                if not resp.ok:
                    return []
                data = json.loads(resp.user_json)
                channels = data.get("channels", {})
                return list(channels.keys())
        except Exception:
            return []

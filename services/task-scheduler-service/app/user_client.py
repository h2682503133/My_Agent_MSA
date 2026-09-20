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
        return [channel for channel, _cid, _prio in self.get_channel_targets(user_id)]

    def get_channel_targets(self, user_id: str) -> list[tuple[str, str, int]]:
        """返回 [(channel, channel_user_id, priority)]，按 priority 升序。

        `channel_user_id` 是**跨渠道投递时必须用的地址**：例如规范用户 `h268` 绑了
        `qq: 2195239690`，转投 qq 时事件要按 QQ 号寻址；反过来把 qq 事件转投 web 时，
        事件 `user_id` 要改写成 web 侧的 `h268`，否则网关按 h268 匹配 SSE 会落空。

        user-service 侧对渠道身份（如 QQ 号）会**透明跟随别名**，所以这里直接传
        `event.user_id` 即可，不需要额外解析。
        """
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
                if not isinstance(channels, dict):
                    return []
                items: list[tuple[int, str, str]] = []
                for name, entry in channels.items():
                    if not isinstance(entry, dict):
                        continue
                    chan = str(entry.get("channel") or name).strip()
                    if not chan:
                        continue
                    chan_uid = str(entry.get("channel_user_id") or "").strip()
                    try:
                        priority = int(entry.get("priority") or 0)
                    except (TypeError, ValueError):
                        priority = 0
                    items.append((priority, chan, chan_uid))
                items.sort(key=lambda x: (x[0], x[1]))
                return [(chan, chan_uid, prio) for prio, chan, chan_uid in items]
        except Exception:
            return []

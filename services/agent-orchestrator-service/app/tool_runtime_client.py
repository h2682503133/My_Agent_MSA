import re
import sys
from pathlib import Path

import grpc

from app import config
from app.logger import debug_log

GENERATED_DIR = Path(__file__).parent / "generated"
if str(GENERATED_DIR) not in sys.path:
    sys.path.insert(0, str(GENERATED_DIR))

try:
    import tool_runtime_pb2
    import tool_runtime_pb2_grpc
except ImportError:
    tool_runtime_pb2 = None
    tool_runtime_pb2_grpc = None


def _safe_workspace_segment(value: str | None, default: str = "default") -> str:
    """
    把 user_id 转成安全目录名，避免 /、\\、.. 等路径逃逸。
    """
    raw = str(value or default).strip()
    if not raw:
        raw = default

    safe = re.sub(r"[^0-9A-Za-z_.@-]+", "_", raw)
    safe = safe.strip("._") or default

    if safe in {".", ".."}:
        safe = default

    return safe


class ToolRuntimeClient:
    def __init__(self):
        self._stub = None

    def _get_stub(self):
        if self._stub is None:
            channel = grpc.insecure_channel(config.TOOL_RUNTIME_TARGET)
            self._stub = tool_runtime_pb2_grpc.ToolRuntimeStub(channel)
        return self._stub

    def _build_user_workspace_dir(self, user_id: str | None) -> str:
        """
        用户长期工作空间规则：

            /app/workspace/users/<user_id>

        不再使用：

            /app/workspace/tasks/<task_id>
        """
        safe_user_id = _safe_workspace_segment(user_id)
        return str(config.WORKSPACE_DIR / "users" / safe_user_id)

    def execute_tool(
        self,
        task_id: str,
        tool_name: str,
        args: list[str],
        user_id: str = "default",
        session_id: str = "",
    ):
        if tool_runtime_pb2 is None:
            raise RuntimeError("tool_runtime protobuf is not generated")

        workspace_dir = self._build_user_workspace_dir(user_id)

        try:
            request = tool_runtime_pb2.ExecuteToolRequest(
                task_id=task_id,
                tool_name=tool_name,
                skill_name=tool_name,
                args=args,
                kwargs={
                    "user_id": str(user_id or "default"),
                    "session_id": str(session_id or ""),
                    "workspace_scope": "user",
                },
                workspace_dir=workspace_dir,
                timeout_seconds=config.TOOL_TIMEOUT_SECONDS,
            )
            response = self._get_stub().ExecuteTool(
                request,
                timeout=config.TOOL_TIMEOUT_SECONDS + 10,
            )
            return {
                "ok": response.ok,
                "output": response.output,
                "artifacts": [
                    {
                        "type": artifact.type,
                        "local_path": artifact.local_path,
                        "asset_url": artifact.asset_url,
                    }
                    for artifact in response.artifacts
                ],
                "logs": response.logs,
                "error": response.error,
            }
        except Exception as exc:
            debug_log(f"ExecuteTool failed: {exc}")
            return {
                "ok": False,
                "output": "",
                "artifacts": [],
                "logs": "",
                "error": str(exc),
            }

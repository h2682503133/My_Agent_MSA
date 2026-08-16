"""tool-runtime-service gRPC 代理客户端。"""

import os
import sys
from pathlib import Path

import grpc

GENERATED_DIR = Path(__file__).parent / "proto_gen"
if str(GENERATED_DIR) not in sys.path:
    sys.path.insert(0, str(GENERATED_DIR))

try:
    import tool_runtime_pb2
    import tool_runtime_pb2_grpc
except ImportError:
    tool_runtime_pb2 = None
    tool_runtime_pb2_grpc = None


class ToolClient:
    def __init__(self, target: str | None = None):
        self.target = target or os.getenv(
            "TOOL_RUNTIME_GRPC_TARGET",
            "tool-runtime-service.agent.svc.cluster.local:5303",
        )

    def _ensure_imports(self):
        if tool_runtime_pb2 is None or tool_runtime_pb2_grpc is None:
            raise RuntimeError(
                "tool_runtime gRPC stubs not generated. "
                "Run generate_proto.sh with tool_runtime.proto in proto/."
            )

    async def _execute_tool(
        self,
        tool_name: str,
        args: list[str] | None = None,
        kwargs: dict[str, str] | None = None,
        timeout: int = 30,
        workspace_dir: str | None = None,
    ) -> dict:
        self._ensure_imports()
        try:
            channel_options = [
                ("grpc.max_send_message_length", 64 * 1024 * 1024),
                ("grpc.max_receive_message_length", 64 * 1024 * 1024),
            ]
            async with grpc.aio.insecure_channel(self.target, options=channel_options) as channel:
                stub = tool_runtime_pb2_grpc.ToolRuntimeStub(channel)
                req = tool_runtime_pb2.ExecuteToolRequest(
                    task_id="gateway",
                    tool_name=tool_name,
                    args=args or [],
                    kwargs=kwargs or {},
                    timeout_seconds=timeout,
                    workspace_dir=workspace_dir or "",
                )
                # ExecuteTool 是 server-streaming：执行过程中的 message/image
                # 事件对 gateway 直接调用无意义，只取最后的 done 事件。
                async for event in stub.ExecuteTool(req):
                    if event.event_type == "done":
                        return {
                            "ok": not bool(event.error),
                            "output": event.output,
                            "error": event.error,
                            "logs": event.logs,
                        }
                return {"ok": False, "output": "", "error": "tool-runtime 未返回最终结果", "logs": ""}
        except Exception as exc:
            return {"ok": False, "output": "", "error": str(exc), "logs": ""}

    async def list_workspace(self, workspace_dir: str | None = None) -> dict:
        return await self._execute_tool("list-workspace", workspace_dir=workspace_dir)

    async def file_read(self, path: str, workspace_dir: str | None = None) -> dict:
        return await self._execute_tool("file-read", kwargs={"path": path}, workspace_dir=workspace_dir)

    async def file_write(self, path: str, text: str, workspace_dir: str | None = None) -> dict:
        return await self._execute_tool(
            "file-write",
            kwargs={"path": path, "text": text},
            workspace_dir=workspace_dir,
        )

    async def file_upload(self, path: str, data_base64: str, workspace_dir: str | None = None) -> dict:
        return await self._execute_tool(
            "file-upload",
            kwargs={"path": path, "data": data_base64},
            workspace_dir=workspace_dir,
            timeout=120,
        )

    async def delete_file(self, path: str, workspace_dir: str | None = None) -> dict:
        return await self._execute_tool("delete-file", kwargs={"path": path}, workspace_dir=workspace_dir)


def build_tool_client() -> ToolClient:
    return ToolClient()

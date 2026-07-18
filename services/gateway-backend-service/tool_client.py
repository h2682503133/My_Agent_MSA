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
            async with grpc.aio.insecure_channel(self.target) as channel:
                stub = tool_runtime_pb2_grpc.ToolRuntimeStub(channel)
                req = tool_runtime_pb2.ExecuteToolRequest(
                    task_id="gateway",
                    tool_name=tool_name,
                    args=args or [],
                    kwargs=kwargs or {},
                    timeout_seconds=timeout,
                    workspace_dir=workspace_dir or "",
                )
                resp = await stub.ExecuteTool(req)
                return {
                    "ok": resp.ok,
                    "output": resp.output,
                    "error": resp.error,
                    "logs": resp.logs,
                }
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

    async def delete_file(self, path: str, workspace_dir: str | None = None) -> dict:
        return await self._execute_tool("delete-file", kwargs={"path": path}, workspace_dir=workspace_dir)


def build_tool_client() -> ToolClient:
    return ToolClient()

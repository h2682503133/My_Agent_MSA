import asyncio
import base64
import json
import os
import re
from pathlib import Path
from typing import List

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from conversations import conversation_manager
from scheduler_client import build_scheduler_client
from schemas import (
    BindChannelRequest,
    ConversationSummary,
    CreateConversationRequest,
    FrontendMessage,
    LoginRequest,
    LoginResponse,
    UserProfileUpdate,
    WorkspaceFileWrite,
)
from sse_hub import SSEHub
from tool_client import build_tool_client
from user_client import build_user_client


APP_NAME = "gateway-backend-service"

app = FastAPI(title=APP_NAME)

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ALLOW_ORIGINS", "*").split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

sse_hub = SSEHub()
scheduler_client = build_scheduler_client()
user_client = build_user_client()
tool_client = build_tool_client()
_consumer_task = None


def build_session_id(user_id: str) -> str:
    return f"web_{user_id}"


def get_whitelist() -> List[str]:
    raw = os.getenv("USER_WHITELIST", "").strip()
    if not raw:
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


def ensure_allowed_user(user_id: str) -> None:
    whitelist = get_whitelist()
    if whitelist and user_id not in whitelist:
        raise HTTPException(status_code=403, detail="user is not in whitelist")


async def scheduler_event_consumer() -> None:
    subscriber_id = os.getenv("SUBSCRIBER_ID", "web-gateway-1")

    async for event in scheduler_client.subscribe_events(
        subscriber_id=subscriber_id,
        channels=["web"],
    ):
        await sse_hub.publish(event)

        # Store assistant replies in conversation history
        if event.type in ("assistant_message", "task_failed") and event.user_id:
            agent_id = event.metadata.get("agent_id") or "main"
            conversation_manager.add_message(
                user_id=event.user_id,
                agent_id=agent_id,
                msg={
                    "role": "agent" if event.type == "assistant_message" else "system",
                    "content": event.text or event.error or "",
                    "task_id": event.task_id,
                    "images": event.images,
                },
            )


@app.on_event("startup")
async def on_startup() -> None:
    global _consumer_task
    _consumer_task = asyncio.create_task(scheduler_event_consumer())


@app.on_event("shutdown")
async def on_shutdown() -> None:
    global _consumer_task
    if _consumer_task and not _consumer_task.done():
        _consumer_task.cancel()
        try:
            await _consumer_task
        except asyncio.CancelledError:
            pass


# ═══════════════════════════════════════════════════════════════
# 健康检查
# ═══════════════════════════════════════════════════════════════
@app.get("/api/health")
async def health():
    return {
        "ok": True,
        "service": APP_NAME,
        "scheduler_client_mode": os.getenv("SCHEDULER_CLIENT_MODE", "grpc"),
        "scheduler_target": os.getenv(
            "SCHEDULER_GRPC_TARGET",
            "task-scheduler-service.agent.svc.cluster.local:5100",
        ),
    }


# ═══════════════════════════════════════════════════════════════
# 登录
# ═══════════════════════════════════════════════════════════════
@app.post("/api/login", response_model=LoginResponse)
async def login(req: LoginRequest):
    user_id = req.user_id.strip()

    if not user_id:
        raise HTTPException(status_code=400, detail="missing user_id")

    ensure_allowed_user(user_id)

    session_id = req.session_id or build_session_id(user_id)

    # Auto-bind web channel on login
    try:
        await user_client.bind_channel(
            user_id=user_id,
            channel="web",
            channel_user_id=user_id,
            priority=0,
        )
    except Exception:
        pass

    return LoginResponse(ok=True, user_id=user_id, session_id=session_id)


# ═══════════════════════════════════════════════════════════════
# 消息（支持 agent_id）
# ═══════════════════════════════════════════════════════════════
@app.post("/api/messages")
async def create_message(req: FrontendMessage):
    user_id = req.user_id.strip()
    content = req.content.strip()
    agent_id = req.agent_id or "main"

    if not user_id:
        raise HTTPException(status_code=400, detail="missing user_id")

    if not content:
        raise HTTPException(status_code=400, detail="missing content")

    ensure_allowed_user(user_id)

    if not req.session_id:
        req.session_id = build_session_id(user_id)

    req.agent_id = agent_id
    req.metadata = dict(req.metadata)
    req.metadata["agent_id"] = agent_id

    result = await scheduler_client.create_task(req)

    if not result.ok:
        raise HTTPException(
            status_code=500,
            detail=result.error or "failed to create task",
        )

    conversation_manager.add_message(
        user_id=user_id,
        agent_id=agent_id,
        msg={
            "role": "user",
            "content": content,
            "task_id": result.task_id,
            "images": list(req.images or []),
        },
    )

    return result.model_dump()


# ═══════════════════════════════════════════════════════════════
# SSE 事件流（支持 agent_id 过滤）
# ═══════════════════════════════════════════════════════════════
@app.get("/api/events")
async def events(
    user_id: str = Query(..., min_length=1),
    session_id: str = Query(default=""),
    agent_id: str = Query(default=""),
):
    ensure_allowed_user(user_id)

    async def stream():
        async for item in sse_hub.event_stream(
            user_id=user_id,
            agent_id=agent_id or None,
        ):
            yield item

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ═══════════════════════════════════════════════════════════════
# 智能体列表
# ═══════════════════════════════════════════════════════════════
@app.get("/api/agents")
async def list_agents(
    user_id: str = Query(..., min_length=1),
):
    ensure_allowed_user(user_id)

    config_path = os.getenv("AGENT_LIST_PATH", "/app/config/agent_list.json")
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {"agents": [], "error": f"agent_list.json not found at {config_path}"}
    except json.JSONDecodeError:
        return {"agents": [], "error": "agent_list.json is not valid JSON"}

    if "default" in data:
        default_agents = data["default"]
        if "agents" in default_agents:
            default_agents = default_agents["agents"]
        agent_ids = sorted(default_agents.keys())
    else:
        # legacy flat format
        agent_ids = sorted(k for k in data.keys() if k not in ("users", "default"))

    return {"agents": [{"agent_id": aid, "is_default": aid == "main"} for aid in agent_ids]}


# ═══════════════════════════════════════════════════════════════
# 对话管理
# ═══════════════════════════════════════════════════════════════
@app.get("/api/conversations")
async def list_conversations(
    user_id: str = Query(..., min_length=1),
):
    ensure_allowed_user(user_id)

    convs = conversation_manager.list_conversations(user_id)
    return {"conversations": convs}


@app.post("/api/conversations")
async def create_conversation(
    req: CreateConversationRequest,
    user_id: str = Query(..., min_length=1),
):
    ensure_allowed_user(user_id)

    agent_id = req.agent_id.strip() or "main"
    conv = conversation_manager.get_or_create(user_id, agent_id)
    return ConversationSummary(
        agent_id=conv.agent_id,
        user_id=conv.user_id,
        message_count=len(conv.messages),
        created_at=conv.created_at,
        last_active=conv.last_active,
    ).model_dump()


@app.delete("/api/conversations/{agent_id}")
async def delete_conversation(
    agent_id: str,
    user_id: str = Query(..., min_length=1),
):
    ensure_allowed_user(user_id)

    deleted = conversation_manager.delete_conversation(user_id, agent_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="conversation not found")
    return {"ok": True, "agent_id": agent_id}

@app.get("/api/conversations/{agent_id}/messages")
async def get_conversation_messages(
    agent_id: str,
    user_id: str = Query(..., min_length=1),
):
    ensure_allowed_user(user_id)

    conv = conversation_manager.get_conversation(user_id, agent_id)
    if not conv:
        raise HTTPException(status_code=404, detail="conversation not found")

    return {
        "agent_id": conv.agent_id,
        "user_id": conv.user_id,
        "messages": conv.messages,
        "message_count": len(conv.messages),
        "created_at": conv.created_at,
        "last_active": conv.last_active,
    }


# ═══════════════════════════════════════════════════════════════
# 用户 & 渠道
# ═══════════════════════════════════════════════════════════════
@app.get("/api/user/profile")
async def get_user_profile(
    user_id: str = Query(..., min_length=1),
):
    ensure_allowed_user(user_id)

    result = await user_client.get_user(user_id)
    if not result.get("ok"):
        return {"ok": False, "user_id": user_id, "error": result.get("error", "user not found")}

    try:
        user_data = json.loads(result.get("user_json", "{}"))
    except json.JSONDecodeError:
        user_data = {}

    return {
        "ok": True,
        "user_id": user_id,
        "profile": user_data,
        "channels": user_data.get("channels", {}),
    }


@app.put("/api/user/profile")
async def update_user_profile(
    req: UserProfileUpdate,
    user_id: str = Query(..., min_length=1),
):
    ensure_allowed_user(user_id)

    if req.user_json is not None:
        try:
            json.loads(req.user_json)
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="user_json is not valid JSON")

    result = await user_client.upsert_user(user_id, req.user_json or "{}")
    return result


@app.post("/api/user/channels")
async def bind_channel(
    req: BindChannelRequest,
    user_id: str = Query(..., min_length=1),
):
    ensure_allowed_user(user_id)

    result = await user_client.bind_channel(
        user_id=user_id,
        channel=req.channel,
        channel_user_id=req.channel_user_id,
        priority=req.priority,
    )
    return result


@app.delete("/api/user/channels/{channel}")
async def unbind_channel(
    channel: str,
    user_id: str = Query(..., min_length=1),
):
    ensure_allowed_user(user_id)

    result = await user_client.unbind_channel(user_id, channel)
    return result


# ═══════════════════════════════════════════════════════════════
# 工作空间
# ═══════════════════════════════════════════════════════════════
WORKSPACE_USERS_BASE = os.getenv("WORKSPACE_USERS_BASE", "/app/workspace/users")


def _user_workspace(user_id: str) -> str:
    return f"{WORKSPACE_USERS_BASE}/{user_id}"

def _clean_path(path: str) -> str:
    return path.lstrip("/")

@app.get("/api/workspace/files")
async def list_workspace_files(
    user_id: str = Query(..., min_length=1),
    path: str = Query(default=""),
):
    ensure_allowed_user(user_id)

    result = await tool_client.list_workspace(workspace_dir=_user_workspace(user_id))
    raw = result.get("output", "")

    # Parse all entries from tool-runtime output
    all_entries: list[dict] = []
    for line in raw.split("\n"):
        line = line.strip()
        if not line:
            continue
        if line.startswith("[dir]"):
            name = line[5:].strip()
            all_entries.append({"name": name, "path": name, "type": "dir", "size": 0})
        elif line.startswith("[file]"):
            parts = line[6:].rsplit("(", 1)
            name = parts[0].strip()
            try:
                size_str = parts[1].replace("bytes)", "").strip() if len(parts) > 1 else "0"
                size = int(size_str)
            except (ValueError, IndexError):
                size = 0
            all_entries.append({"name": name, "path": name, "type": "file", "size": size})

    # Build a set of all directory paths for quick lookup
    dir_paths = {e["path"] for e in all_entries if e["type"] == "dir"}
    # Also include implicit directories (parent paths of any entry)
    for e in all_entries:
        parts = e["path"].split("/")
        for i in range(1, len(parts)):
            dir_paths.add("/".join(parts[:i]))

    # Normalize current path
    current = _clean_path(path)
    prefix = (current + "/") if current else ""

    # Find only direct children of current path
    seen: set[str] = set()
    files: list[dict] = []
    for e in all_entries:
        rel = e["path"]
        # Must be under current path, not the path itself
        if not rel.startswith(prefix) or rel == current:
            continue
        # Get the part after the prefix
        sub = rel[len(prefix):]
        # Get the first component (direct child name)
        first = sub.split("/")[0]
        if first in seen:
            continue
        seen.add(first)

        child_path = prefix + first
        is_dir = child_path in dir_paths
        size = e["size"] if e["type"] == "file" and not is_dir else 0
        files.append({
            "name": first,
            "path": child_path,
            "type": "dir" if is_dir else "file",
            "size": size,
        })

    return {"files": files, "ok": result.get("ok", False)}


@app.get("/api/workspace/files/read")
async def read_workspace_file(
    user_id: str = Query(..., min_length=1),
    path: str = Query(..., min_length=1),
    encoding: str = Query(default="utf-8"),
):
    ensure_allowed_user(user_id)

    result = await tool_client.file_read(path=_clean_path(path), workspace_dir=_user_workspace(user_id))
    return {
        "ok": result.get("ok", False),
        "path": path,
        "encoding": encoding,
        "content": result.get("output", ""),
        "error": result.get("error", ""),
    }


@app.get("/api/workspace/files/raw")
async def raw_workspace_file(
    user_id: str = Query(..., min_length=1),
    path: str = Query(..., min_length=1),
):
    """返回原始文件内容（用于图片等二进制预览）。"""
    ensure_allowed_user(user_id)

    result = await tool_client.file_read(path=_clean_path(path), workspace_dir=_user_workspace(user_id))
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("error", "file not found"))

    content = result.get("output", "")

    # 推断 MIME 类型
    ext = Path(path).suffix.lower()
    mime_map = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".svg": "image/svg+xml",
        ".bmp": "image/bmp",
        ".ico": "image/x-icon",
        ".txt": "text/plain",
        ".md": "text/markdown",
        ".json": "application/json",
        ".yaml": "text/yaml",
        ".yml": "text/yaml",
        ".py": "text/x-python",
        ".html": "text/html",
        ".css": "text/css",
        ".js": "text/javascript",
        ".xml": "text/xml",
        ".log": "text/plain",
    }
    media_type = mime_map.get(ext, "application/octet-stream")

    from fastapi.responses import Response
    return Response(content=content.encode("utf-8"), media_type=media_type)

@app.get("/api/assets/{filename:path}")
async def serve_asset(filename: str):
    """Serve shared assets (images etc.) from the assets PVC."""
    import os as _os
    assets_root = _os.getenv("ASSETS_DIR", "/app/assets")
    filepath = Path(assets_root) / filename

    try:
        filepath.resolve().relative_to(Path(assets_root).resolve())
    except ValueError:
        raise HTTPException(status_code=403, detail="path traversal denied")

    if not filepath.exists() or not filepath.is_file():
        raise HTTPException(status_code=404, detail="asset not found")

    ext = filepath.suffix.lower()
    mime_map = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".svg": "image/svg+xml",
        ".bmp": "image/bmp",
        ".ico": "image/x-icon",
    }
    media_type = mime_map.get(ext, "application/octet-stream")

    from fastapi.responses import FileResponse
    return FileResponse(str(filepath), media_type=media_type)


@app.post("/api/workspace/files/write")
async def write_workspace_file(
    req: WorkspaceFileWrite,
    user_id: str = Query(..., min_length=1),
    path: str = Query(..., min_length=1),
):
    ensure_allowed_user(user_id)

    result = await tool_client.file_write(path=_clean_path(path), text=req.text, workspace_dir=_user_workspace(user_id))
    return {
        "ok": result.get("ok", False),
        "path": path,
        "output": result.get("output", ""),
        "error": result.get("error", ""),
    }


@app.post("/api/workspace/files/upload")
async def upload_workspace_file(
    file: UploadFile = File(...),
    user_id: str = Query(..., min_length=1),
    path: str = Query(default=""),
):
    """上传文件到工作空间。path 为目标目录（空为根），文件名保留相对路径。"""
    ensure_allowed_user(user_id)

    filename = (file.filename or "").replace("\\", "/")
    if not filename:
        raise HTTPException(status_code=400, detail="missing filename")
    segments = [seg for seg in filename.split("/") if seg not in ("", ".")]
    if not segments or any(seg == ".." for seg in segments):
        raise HTTPException(status_code=400, detail="invalid filename")
    rel_name = "/".join(segments)

    base = _clean_path(path)
    rel = f"{base}/{rel_name}" if base else rel_name
    rel = _clean_path(rel)

    data = await file.read()
    if len(data) > 48 * 1024 * 1024:
        raise HTTPException(status_code=413, detail=f"file too large: {len(data)} bytes > 48MB")

    data_b64 = base64.b64encode(data).decode("ascii")
    result = await tool_client.file_upload(path=rel, data_base64=data_b64, workspace_dir=_user_workspace(user_id))
    return {
        "ok": result.get("ok", False),
        "path": rel,
        "size": len(data),
        "output": result.get("output", ""),
        "error": result.get("error", ""),
    }


@app.delete("/api/workspace/files")
async def delete_workspace_file(
    user_id: str = Query(..., min_length=1),
    path: str = Query(..., min_length=1),
):
    ensure_allowed_user(user_id)

    result = await tool_client.delete_file(path=_clean_path(path), workspace_dir=_user_workspace(user_id))
    return {
        "ok": result.get("ok", False),
        "path": path,
        "output": result.get("output", ""),
        "error": result.get("error", ""),
    }


# ═══════════════════════════════════════════════════════════════
# 端口代理（agent 暴露端口）
# ═══════════════════════════════════════════════════════════════
def _parse_port_range(spec: str) -> tuple[int, int]:
    spec = (spec or "").strip()
    if not spec:
        return 5800, 5899
    low, _, high = spec.partition("-")
    try:
        low_i = int(low.strip())
    except (TypeError, ValueError):
        low_i = 5800
    try:
        high_i = int(high.strip()) if high.strip() else low_i
    except (TypeError, ValueError):
        high_i = 5800
    return low_i, max(low_i, high_i)


_HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "content-length",
}


@app.api_route("/api/port/{port}/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
async def proxy_exposed_port(port: int, path: str, request: Request):
    low, high = _parse_port_range(os.getenv("PORT_PROXY_RANGE", "5800-5899"))
    if not (low <= port <= high):
        raise HTTPException(status_code=403, detail=f"port {port} out of allowed range {low}-{high}")

    import httpx

    target = f"http://tool-runtime-direct:{port}/{path}"
    headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP_HEADERS}
    body = await request.body()

    client = httpx.AsyncClient(timeout=300)
    req = client.build_request(request.method, target, params=request.query_params, headers=headers, content=body)
    resp = await client.send(req, stream=True)
    resp_headers = {k: v for k, v in resp.headers.items() if k.lower() not in _HOP_HEADERS}

    async def _proxy_stream():
        try:
            async for chunk in resp.aiter_bytes():
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()

    return StreamingResponse(_proxy_stream(), status_code=resp.status_code, headers=resp_headers)


# ═══════════════════════════════════════════════════════════════
# 日志
# ═══════════════════════════════════════════════════════════════
@app.get("/api/logs/orchestrator")
async def get_orchestrator_logs(
    user_id: str = Query(..., min_length=1),
    lines: int = Query(default=200, ge=1, le=2000),
    agent_id: str = Query(default=""),
):
    """获取 agent-orchestrator-service 中当前用户的相关日志。

    通过 kubectl logs 读取 pod 日志并 grep 过滤 user_id。
    需要 dashboard-sa 或同等 RBAC 权限。
    """
    ensure_allowed_user(user_id)

    namespace = os.getenv("K8S_NAMESPACE", "agent")
    label_selector = "app=agent-orchestrator-service"

    cmd = [
        "kubectl", "logs",
        "-n", namespace,
        "-l", label_selector,
        "--tail", str(lines),
        "--timestamps",
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(),
            timeout=10,
        )
    except asyncio.TimeoutError:
        return {"ok": False, "error": "kubectl logs timed out", "lines": []}
    except FileNotFoundError:
        return {"ok": False, "error": "kubectl not found in PATH", "lines": []}

    if proc.returncode != 0:
        return {
            "ok": False,
            "error": stderr.decode("utf-8", errors="replace").strip(),
            "lines": [],
        }

    raw = stdout.decode("utf-8", errors="replace")
    filtered: list[str] = []
    # kubectl --timestamps 会给每一行加时间戳，需要按 app 日志前缀分组
    # 新条目特征：kubectl时间戳 + [HH:MM:SS] [chat] 或 [HH:MM:SS] [debug]
    _LOG_PREFIX_RE = re.compile(r"\[\d{2}:\d{2}:\d{2}\] \[(chat|debug)\]")
    current_entry: list[str] = []
    for line in raw.split("\n"):
        if _LOG_PREFIX_RE.search(line):
            # 新条目开始 → 先处理上一个条目
            if current_entry:
                if user_id in current_entry[0]:
                    if not agent_id or agent_id in current_entry[0]:
                        filtered.append("\n".join(current_entry))
            current_entry = [line.rstrip()]
        else:
            if current_entry:
                current_entry.append(line.rstrip())
    # 处理最后一条
    if current_entry:
        if user_id in current_entry[0]:
            if not agent_id or agent_id in current_entry[0]:
                filtered.append("\n".join(current_entry))

    return {
        "ok": True,
        "user_id": user_id,
        "agent_id": agent_id or None,
        "total_lines": len(filtered),
        "lines": filtered[-lines:],
    }

"""
dashboard-service - My_Agent MSA 管理控制面板
FastAPI 后端 + 静态前端
"""

import json
import os
import subprocess
import asyncio
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import uvicorn

app = FastAPI(title="My_Agent Dashboard")

# ─── 配置 ───────────────────────────────────────────────────
CONFIG_ROOT = Path(os.environ.get("CONFIG_ROOT", "/app/config"))
STATIC_DIR = Path(__file__).parent.parent / "static"
NAMESPACE = os.environ.get("NAMESPACE", "agent")

# ─── Pydantic models ────────────────────────────────────────

class ConfigUpdate(BaseModel):
    path: str          # 相对于 CONFIG_ROOT 的路径
    content: str       # 新内容（JSON 字符串或纯文本）

class SystemPromptFile(BaseModel):
    agent: str
    filename: str
    content: str = ""

class DeployRequest(BaseModel):
    services: list[str]
    path: str = ""     # 项目根路径（可选，默认使用内置 deploy 脚本）


# ─── 配置管理 ────────────────────────────────────────────────

@app.get("/api/config")
async def list_configs():
    """列出所有配置文件"""
    files = []
    if CONFIG_ROOT.exists():
        for f in CONFIG_ROOT.rglob("*"):
            if f.is_file() and not f.name.startswith("."):
                rel = str(f.relative_to(CONFIG_ROOT)).replace("\\", "/")
                if "system_prompt" in rel:
                    continue
                files.append({
                    "path": rel,
                    "size": f.stat().st_size,
                    "editable": f.suffix in (".json", ".md", ".txt", ".yaml", ".yml", ".toml", ".cfg", ".conf")
                })
    return {"files": sorted(files, key=lambda x: x["path"])}


@app.get("/api/config/{path:path}")
async def read_config(path: str):
    """读取配置文件内容"""
    full = CONFIG_ROOT / path
    if not full.exists():
        raise HTTPException(404, "文件不存在")
    if full.is_dir():
        raise HTTPException(400, "不能读取目录")
    content = full.read_text(encoding="utf-8")
    return {"path": path, "content": content}


@app.put("/api/config/{path:path}")
async def write_config(path: str, body: ConfigUpdate):
    """写入配置文件"""
    full = CONFIG_ROOT / path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(body.content, encoding="utf-8")
    return {"ok": True, "path": path}


# ─── Pod 状态 ────────────────────────────────────────────────

@app.get("/api/pods")
async def list_pods():
    """获取所有 Pod 状态"""
    try:
        result = subprocess.run(
            ["kubectl", "-n", NAMESPACE, "get", "pods", "-o", "json"],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode != 0:
            return {"error": result.stderr, "pods": []}
        data = json.loads(result.stdout)
        pods = []
        for item in data.get("items", []):
            name = item["metadata"]["name"]
            status = item["status"]["phase"]
            labels = item["metadata"].get("labels", {})
            deployment = labels.get("app", name.rsplit("-", 2)[0] if "-" in name else name)
            containers = []
            for c in item["status"].get("containerStatuses", []):
                containers.append({
                    "name": c["name"],
                    "ready": c["ready"],
                    "restarts": c["restartCount"],
                    "image": c["image"]
                })
            pods.append({"name": name, "deployment": deployment, "status": status, "containers": containers})
        return {"pods": pods}
    except Exception as e:
        return {"error": str(e), "pods": []}


@app.get("/api/services")
async def list_services():
    """获取所有 Service"""
    try:
        result = subprocess.run(
            ["kubectl", "-n", NAMESPACE, "get", "svc", "-o", "json"],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode != 0:
            return {"error": result.stderr, "services": []}
        data = json.loads(result.stdout)
        svcs = []
        for item in data.get("items", []):
            svcs.append({
                "name": item["metadata"]["name"],
                "type": item["spec"]["type"],
                "clusterIP": item["spec"].get("clusterIP", ""),
                "ports": [f"{p['port']}/{p.get('protocol', 'TCP')}" for p in item["spec"].get("ports", [])]
            })
        return {"services": svcs}
    except Exception as e:
        return {"error": str(e), "services": []}


# ─── 日志查看 ────────────────────────────────────────────────

@app.get("/api/logs")
async def get_logs(pod: str, tail: int = 200, container: Optional[str] = None):
    """获取 Pod 日志"""
    cmd = ["kubectl", "-n", NAMESPACE, "logs", pod, f"--tail={tail}"]
    if container:
        cmd.extend(["-c", container])
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        return {"ok": True, "logs": result.stdout, "error": result.stderr if result.returncode else ""}
    except Exception as e:
        return {"ok": False, "error": str(e), "logs": ""}


@app.get("/api/logs/stream")
async def stream_logs(pod: str, tail: int = 10, container: Optional[str] = None):
    """实时流式日志（SSE）"""
    cmd = ["kubectl", "-n", NAMESPACE, "logs", "-f", pod, f"--tail={tail}"]
    if container:
        cmd.extend(["-c", container])

    async def generate():
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        try:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                yield f"data: {line.decode('utf-8', errors='replace').strip()}\n\n"
        except asyncio.CancelledError:
            proc.terminate()
            await proc.wait()

    return StreamingResponse(generate(), media_type="text/event-stream")


# ─── 部署操作 ────────────────────────────────────────────────

@app.post("/api/restart")
async def restart_service(service: str = Query(...)):
    """重启指定 Deployment"""
    try:
        result = subprocess.run(
            ["kubectl", "-n", NAMESPACE, "rollout", "restart", f"deployment/{service}"],
            capture_output=True, text=True, timeout=30
        )
        return {"ok": result.returncode == 0, "output": result.stdout, "error": result.stderr}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/api/deploy/script")
async def generate_deploy_script(
    services: str = Query("", description="逗号分隔的服务名，空则全部")
):
    """生成一键部署脚本"""
    selected = [s.strip() for s in services.split(",") if s.strip()] if services else []

    all_services = {
        "dashboard-service":            {"dir": "dashboard-service",            "tag": "v1"},
        "agent-orchestrator-service":   {"dir": "agent-orchestrator-service",   "tag": "v11"},
        "task-scheduler-service":       {"dir": "task-scheduler-service",       "tag": "v5"},
        "timer-task-service":           {"dir": "timer-task-service",           "tag": "v2"},
        "gateway-backend-service":      {"dir": "gateway-backend-service",      "tag": "v4"},
        "qq-llbot-service":             {"dir": "qq-llbot-service",             "tag": "v1"},
        "model-proxy-service":          {"dir": "model-proxy-service",          "tag": "v3"},
        "openviking-context-service":   {"dir": "openviking-context-service",   "tag": "v17"},
        "tool-runtime-service":         {"dir": "tool-runtime-service",         "tag": "v1"},
        "user-service":                 {"dir": "user-service",                 "tag": "v1"},
        "frontend-service":             {"dir": "frontend-service",             "tag": "v1"},
    }

    if not selected:
        selected = list(all_services.keys())

    lines = ["@echo off", "REM My_Agent MSA 一键部署", ""]
    for svc in selected:
        if svc in all_services:
            info = all_services[svc]
            lines.append(f"echo 构建 {svc}...")
            lines.append(f"docker build -t agent/{svc}:{info['tag']} {info['dir']}/")
            lines.append(f"kubectl apply -f deploy/services/{svc}.yaml")
            lines.append("")

    return {"script": "\n".join(lines), "services": selected}


# ─── System Prompt 管理 ──────────────────────────────────────

SYSTEM_PROMPT_ROOT = CONFIG_ROOT / "orchestrator" / "system_prompt"

@app.get("/api/system_prompt")
async def list_system_prompts():
    agents = []
    global_files = []
    if SYSTEM_PROMPT_ROOT.exists():
        for d in sorted(SYSTEM_PROMPT_ROOT.iterdir()):
            if d.is_file() and d.suffix == ".md" and not d.name.startswith("."):
                global_files.append({"name": d.name, "size": d.stat().st_size})
            elif d.is_dir() and not d.name.startswith("."):
                files = [{"name": f.name, "size": f.stat().st_size}
                         for f in sorted(d.iterdir()) if f.is_file() and f.suffix == ".md"]
                agents.append({"name": d.name, "files": files})
    return {"agents": agents, "global_files": global_files}

@app.post("/api/system_prompt/agent")
async def create_agent(name: str = Query(...)):
    d = SYSTEM_PROMPT_ROOT / name
    if d.exists():
        raise HTTPException(400, "智能体已存在")
    d.mkdir(parents=True)
    return {"ok": True, "name": name}

@app.delete("/api/system_prompt/agent/{name}")
async def delete_agent(name: str):
    import shutil
    d = SYSTEM_PROMPT_ROOT / name
    if not d.exists():
        raise HTTPException(404, "智能体不存在")
    shutil.rmtree(d)
    return {"ok": True}

@app.get("/api/system_prompt/global/{filename:path}")
async def read_sp_global(filename: str):
    """读取全局 .md 文件（如 GLOBAL_SETTING.md）"""
    fp = SYSTEM_PROMPT_ROOT / filename
    if not fp.exists():
        raise HTTPException(404, "文件不存在")
    return {"agent": "", "filename": filename, "content": fp.read_text("utf-8")}

@app.get("/api/system_prompt/{agent}/{filename:path}")
async def read_sp(agent: str, filename: str):
    fp = SYSTEM_PROMPT_ROOT / agent / filename
    if not fp.exists():
        raise HTTPException(404, "文件不存在")
    return {"agent": agent, "filename": filename, "content": fp.read_text("utf-8")}

@app.put("/api/system_prompt/file")
async def write_sp(body: SystemPromptFile):
    fp = SYSTEM_PROMPT_ROOT / body.agent / body.filename
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text(body.content, encoding="utf-8")
    return {"ok": True}

@app.delete("/api/system_prompt/file/{agent}/{filename:path}")
async def delete_sp(agent: str, filename: str):
    fp = SYSTEM_PROMPT_ROOT / agent / filename
    if not fp.exists():
        raise HTTPException(404, "文件不存在")
    fp.unlink()
    return {"ok": True}

# ─── 静态页面 ────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    index_file = STATIC_DIR / "index.html"
    if index_file.exists():
        return index_file.read_text(encoding="utf-8")
    return "<h1>Dashboard</h1><p>index.html not found</p>"


# ─── 入口 ────────────────────────────────────────────────────

def main():
    uvicorn.run("app.main:app", host="0.0.0.0", port=5601, reload=False)


if __name__ == "__main__":
    main()

"""
dashboard-service - My_Agent MSA 管理控制面板
FastAPI 后端 + 静态前端
"""

import json
import os
import subprocess
import shutil
import asyncio
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, UploadFile
from fastapi.responses import HTMLResponse, StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import uvicorn

app = FastAPI(title="My_Agent Dashboard")

# ─── 配置 ───────────────────────────────────────────────────
CONFIG_ROOT = Path(os.environ.get("CONFIG_ROOT", "/app/config"))
STATIC_DIR = Path(__file__).parent.parent / "static"
NAMESPACE = os.environ.get("NAMESPACE", "agent")
SESSION_ROOT = Path(os.environ.get("SESSION_ROOT", "/app/session-data/workspace/viking/my-agent/session"))
PASSWORD_FILE = CONFIG_ROOT / "dashboard_password.json"

# ─── Pydantic models ────────────────────────────────────────

class ConfigUpdate(BaseModel):
    path: str
    content: str

class SystemPromptFile(BaseModel):
    agent: str
    filename: str
    content: str = ""

class DeployRequest(BaseModel):
    services: list[str]
    path: str = ""

class MessageDelete(BaseModel):
    lines: list[int]

class LoginRequest(BaseModel):
    password: str

class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str


# ─── 密码管理 ────────────────────────────────────────────────

def _get_password() -> str:
    """读取密码，默认 123456"""
    if PASSWORD_FILE.exists():
        try:
            data = json.loads(PASSWORD_FILE.read_text())
            return data.get("password", "123456")
        except Exception:
            pass
    return "123456"

def _save_password(password: str):
    """保存密码到 PV config"""
    PASSWORD_FILE.parent.mkdir(parents=True, exist_ok=True)
    PASSWORD_FILE.write_text(json.dumps({"password": password}))

@app.post("/api/auth/login")
async def login(body: LoginRequest):
    if body.password == _get_password():
        return {"ok": True}
    raise HTTPException(401, "密码错误")

@app.post("/api/auth/change-password")
async def change_password(body: ChangePasswordRequest):
    if body.old_password != _get_password():
        raise HTTPException(403, "旧密码错误")
    if len(body.new_password) < 1:
        raise HTTPException(400, "新密码不能为空")
    _save_password(body.new_password)
    return {"ok": True}


# ─── 配置管理 ────────────────────────────────────────────────

@app.get("/api/config")
async def list_configs():
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
    full = CONFIG_ROOT / path
    if not full.exists():
        raise HTTPException(404, "文件不存在")
    if full.is_dir():
        raise HTTPException(400, "不能读取目录")
    content = full.read_text(encoding="utf-8")
    return {"path": path, "content": content}

@app.put("/api/config/{path:path}")
async def write_config(path: str, body: ConfigUpdate):
    full = CONFIG_ROOT / path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(body.content, encoding="utf-8")
    return {"ok": True, "path": path}


# ─── Pod 状态 ────────────────────────────────────────────────

@app.get("/api/pods")
async def list_pods():
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
                "ports": [f"{p['port']}:{p.get('targetPort', p['port'])}/{p.get('protocol', 'TCP')}"
                          for p in item["spec"].get("ports", [])]
            })
        return {"services": svcs}
    except Exception as e:
        return {"error": str(e), "services": []}


# ─── 重启 ────────────────────────────────────────────────────

@app.post("/api/restart")
async def restart_service(service: str = Query("")):
    if not service:
        raise HTTPException(400, "缺少 service 参数")
    try:
        result = subprocess.run(
            ["kubectl", "-n", NAMESPACE, "rollout", "restart", "deployment", service],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode != 0:
            return {"ok": False, "error": result.stderr.strip()}
        return {"ok": True, "deployment": service}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ─── 日志 ────────────────────────────────────────────────────

@app.get("/api/logs")
async def get_logs(pod: str = Query(""), tail: int = Query(200), container: str = Query("")):
    if not pod:
        raise HTTPException(400, "缺少 pod 参数")
    args = ["kubectl", "-n", NAMESPACE, "logs", pod, f"--tail={tail}"]
    if container:
        args.extend(["-c", container])
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=15)
        return {"logs": result.stdout, "error": result.stderr if result.returncode != 0 else None}
    except Exception as e:
        return {"logs": "", "error": str(e)}

@app.get("/api/logs/stream")
async def stream_logs(pod: str = Query(""), tail: int = Query(200), container: str = Query("")):
    if not pod:
        raise HTTPException(400, "缺少 pod 参数")
    args = ["kubectl", "-n", NAMESPACE, "logs", "-f", pod, f"--tail={tail}"]
    if container:
        args.extend(["-c", container])

    async def generate():
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        try:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                yield f"data: {line.decode('utf-8', errors='replace').rstrip()}\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            proc.kill()
            await proc.wait()
    return StreamingResponse(generate(), media_type="text/event-stream")


# ─── 部署 ────────────────────────────────────────────────────

@app.get("/api/deploy/script")
async def deploy_script(services: str = Query("")):
    svc_list = [s.strip() for s in services.split(",") if s.strip()]
    script = "# 部署脚本\n\n"
    script += "kubectl create namespace agent --dry-run=client -o yaml | kubectl apply -f -\n\n"
    for svc in svc_list:
        yaml_path = f"deploy/services/{svc}.yaml"
        script += f"# 部署 {svc}\n"
        script += f"kubectl apply -f {yaml_path}\n\n"
    script += "echo 部署完成\n"
    return {"script": script}


# ─── System Prompt ──────────────────────────────────────────

SYSTEM_PROMPT_ROOT = CONFIG_ROOT / "orchestrator" / "system_prompt"

@app.get("/api/system_prompt")
async def list_sp():
    agents = []
    global_files = []
    if SYSTEM_PROMPT_ROOT.exists():
        for d in sorted(SYSTEM_PROMPT_ROOT.iterdir()):
            if d.is_dir() and d.name == "global":
                for f in sorted(d.iterdir()):
                    if f.is_file() and not f.name.startswith("."):
                        global_files.append({"name": f.name, "size": f.stat().st_size})
            elif d.is_dir() and not d.name.startswith("."):
                files = []
                for f in sorted(d.iterdir()):
                    if f.is_file() and not f.name.startswith("."):
                        files.append({"name": f.name, "size": f.stat().st_size})
                agents.append({"name": d.name, "files": files})
    return {"agents": agents, "global_files": global_files}

@app.post("/api/system_prompt/agent")
async def sp_add_agent(name: str = Query("")):
    if not name:
        raise HTTPException(400, "缺少 name 参数")
    (SYSTEM_PROMPT_ROOT / name).mkdir(parents=True, exist_ok=True)
    return {"ok": True}

@app.delete("/api/system_prompt/agent/{name}")
async def sp_delete_agent(name: str):
    agent_dir = SYSTEM_PROMPT_ROOT / name
    if not agent_dir.exists():
        raise HTTPException(404, "智能体不存在")
    shutil.rmtree(agent_dir)
    return {"ok": True}

@app.get("/api/system_prompt/global/{filename:path}")
async def read_sp_global(filename: str):
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


# ═══ 二维码 ═════════════════════════════════════════════════

QRCODE_TIME_FILE = STATIC_DIR / "qrcode_time.txt"

@app.post("/api/qrcode")
async def upload_qrcode(file: UploadFile):
    """接收 admin-panel 推送的二维码图片"""
    qr_path = STATIC_DIR / "qrcode.png"
    content = await file.read()
    qr_path.write_bytes(content)
    # 记录更新时间
    tz_utc8 = timezone(timedelta(hours=8))
    now = datetime.now(tz_utc8).strftime("%Y-%m-%d %H:%M:%S")
    QRCODE_TIME_FILE.write_text(now)
    return {"ok": True, "size": len(content), "updated_at": now}

@app.get("/api/qrcode")
async def get_qrcode():
    """返回二维码图片"""
    qr_path = STATIC_DIR / "qrcode.png"
    if not qr_path.exists():
        raise HTTPException(404, "二维码尚未生成，请先启动 LLBot")
    return FileResponse(qr_path, media_type="image/png")

@app.get("/api/qrcode/info")
async def qrcode_info():
    """返回二维码状态信息"""
    qr_path = STATIC_DIR / "qrcode.png"
    exists = qr_path.exists()
    updated_at = ""
    if exists and QRCODE_TIME_FILE.exists():
        updated_at = QRCODE_TIME_FILE.read_text().strip()
    return {"exists": exists, "updated_at": updated_at}


# ═══ Session 管理 ═══════════════════════════════════════════

@app.get("/api/session/users")
async def session_users():
    if not SESSION_ROOT.exists():
        return {"users": []}
    users = []
    for d in sorted(SESSION_ROOT.iterdir()):
        if d.is_dir() and not d.name.startswith("."):
            agent_count = sum(1 for a in d.iterdir() if a.is_dir() and not a.name.startswith("."))
            users.append({"name": d.name, "agent_count": agent_count})
    return {"users": users}

@app.get("/api/session/{user_id}/agents")
async def session_agents(user_id: str):
    user_dir = SESSION_ROOT / user_id
    if not user_dir.exists():
        raise HTTPException(404, "用户不存在")
    agents = []
    for d in sorted(user_dir.iterdir()):
        if d.is_dir() and not d.name.startswith("."):
            msg_file = d / "messages.jsonl"
            msg_count = 0
            if msg_file.exists():
                with open(msg_file, "r", encoding="utf-8") as f:
                    msg_count = sum(1 for _ in f)
            has_history = (d / "history").exists()
            agents.append({"name": d.name, "msg_count": msg_count, "has_history": has_history})
    return {"agents": agents}

@app.get("/api/session/{user_id}/{agent_id}/messages")
async def session_messages(user_id: str, agent_id: str):
    msg_file = SESSION_ROOT / user_id / agent_id / "messages.jsonl"
    if not msg_file.exists():
        return {"messages": [], "total": 0}
    messages = []
    with open(msg_file, "r", encoding="utf-8") as f:
        for i, line in enumerate(f):
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
                msg["_line"] = i
                messages.append(msg)
            except json.JSONDecodeError:
                continue
    return {"messages": messages, "total": len(messages)}

@app.delete("/api/session/{user_id}/{agent_id}/messages")
async def session_delete_messages(user_id: str, agent_id: str, body: MessageDelete):
    msg_file = SESSION_ROOT / user_id / agent_id / "messages.jsonl"
    if not msg_file.exists():
        raise HTTPException(404, "messages.jsonl 不存在")
    lines_to_delete = set(body.lines)
    with open(msg_file, "r", encoding="utf-8") as f:
        all_lines = f.readlines()
    kept = [l for i, l in enumerate(all_lines) if i not in lines_to_delete]
    with open(msg_file, "w", encoding="utf-8") as f:
        f.writelines(kept)
    return {"ok": True, "deleted": len(lines_to_delete & set(range(len(all_lines)))), "remaining": len(kept)}

@app.delete("/api/session/{user_id}/{agent_id}")
async def session_delete_agent(user_id: str, agent_id: str):
    agent_dir = SESSION_ROOT / user_id / agent_id
    if not agent_dir.exists():
        raise HTTPException(404, "智能体目录不存在")
    shutil.rmtree(agent_dir)
    return {"ok": True, "deleted": str(agent_dir)}


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

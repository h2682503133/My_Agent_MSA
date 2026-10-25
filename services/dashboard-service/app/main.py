"""
dashboard-service - My_Agent MSA 管理控制面板
FastAPI 后端 + 静态前端
"""

import json
import os
import re
import subprocess
import shutil
import asyncio
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, UploadFile
from fastapi.responses import HTMLResponse, StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import uvicorn

from app import tool_catalog

app = FastAPI(title="My_Agent Dashboard")

# ─── 配置 ───────────────────────────────────────────────────
CONFIG_ROOT = Path(os.environ.get("CONFIG_ROOT", "/app/config"))
STATIC_DIR = Path(__file__).parent.parent / "static"
# 前端静态资源：css / 各标签页 html 分片 / 各标签页 js 模块（见 static/assets 目录）
ASSETS_DIR = STATIC_DIR / "assets"
NAMESPACE = os.environ.get("NAMESPACE", "agent")
SESSION_ROOT = Path(os.environ.get("SESSION_ROOT", "/app/session-data/workspace/viking/my-agent/user"))
OPENVIKING_SERVER_URL = os.environ.get("OPENVIKING_SERVER_URL", "http://openviking.agent.svc.cluster.local:1933")
USER_SERVICE_URL = os.environ.get("USER_SERVICE_URL", "http://user-service.agent.svc.cluster.local:5204")
OPENVIKING_API_KEY_FILE = Path(os.environ.get("OPENVIKING_API_KEY", "/app/config/openviking/api_key"))
PASSWORD_FILE = CONFIG_ROOT / "dashboard_password.json"
# 杂项设置落盘位置：orchestrator 挂载 subPath=orchestrator/config，
# 该文件在 orchestrator 容器内即 /app/config/system_settings.json
MISC_SETTINGS_PATH = CONFIG_ROOT / "orchestrator" / "config" / "system_settings.json"
DEFAULT_MISC_SETTINGS = {
    "image_receive_enabled": True,
    "main_read_identity": True,
    "shell_restriction_enabled": False,
    "shell_allowed_users": [],
    "mnt_write_restriction_enabled": False,
    "mnt_write_allowed_users": [],
}

# 静态资源挂载（css / js / tabs 分片）；目录不存在时跳过，便于本地只有 index.html 的场景
if ASSETS_DIR.exists():
    app.mount("/assets", StaticFiles(directory=str(ASSETS_DIR)), name="assets")

# ─── Pydantic models ────────────────────────────────────────

class ConfigUpdate(BaseModel):
    path: str
    content: str

class SystemPromptFile(BaseModel):
    agent: str
    filename: str
    content: str = ""

class MessageDelete(BaseModel):
    lines: list[int]

class LoginRequest(BaseModel):
    password: str

class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str

class MiscSettingsUpdate(BaseModel):
    image_receive_enabled: bool | None = None
    main_read_identity: bool | None = None
    shell_restriction_enabled: bool | None = None
    shell_allowed_users: list[str] | None = None
    mnt_write_restriction_enabled: bool | None = None
    mnt_write_allowed_users: list[str] | None = None

class ToolGenerateRequest(BaseModel):
    """按 TOOL.json 勾选结果生成智能体 TOOL.md。

    - `sections` / `tools`：前端勾选结果（推荐；规则由 app/tool_catalog.py 统一实现）
    - `options`：直接给选项块（高级用法，给了就以它为准）
    """
    options: dict | None = None
    sections: list[str] | None = None
    tools: list[str] | None = None
    body: str = ""
    show_title: bool | None = None
    body_position: str | None = None


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


# ─── 杂项设置 ────────────────────────────────────────────────

def _load_misc_settings() -> dict:
    data = {}
    if MISC_SETTINGS_PATH.exists():
        try:
            loaded = json.loads(MISC_SETTINGS_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except Exception:
            pass
    merged = dict(DEFAULT_MISC_SETTINGS)
    merged.update(data)
    return merged

@app.get("/api/misc/settings")
async def get_misc_settings():
    return _load_misc_settings()

@app.put("/api/misc/settings")
async def update_misc_settings(body: MiscSettingsUpdate):
    data = _load_misc_settings()
    payload = body.model_dump(exclude_none=True)
    for key, value in payload.items():
        data[key] = value
    MISC_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    MISC_SETTINGS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, **data}

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
                # PROCESS 存储由智能体维护，不挤占配置管理显示空间
                if "process" in rel.split("/"):
                    continue
                # 世界书由「世界书」页面管理，不挤占配置管理显示空间
                if "world_info" in rel.split("/"):
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


# ─── 世界书（World Info）管理 ────────────────────────────────
# 存储：orchestrator/config/world_info/{world_info.json, groups.json}
#（与 PROCESS 同级；orchestrator/tool-runtime 直接读写同一文件）

WORLD_INFO_ROOT = CONFIG_ROOT / "orchestrator" / "config" / "world_info"
WORLD_INFO_PATH = WORLD_INFO_ROOT / "world_info.json"
WORLD_INFO_GROUPS_PATH = WORLD_INFO_ROOT / "groups.json"

def _load_world_info() -> dict:
    if WORLD_INFO_PATH.exists():
        try:
            data = json.loads(WORLD_INFO_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {"version": 1, "entries": []}

def _save_world_info(data: dict) -> None:
    WORLD_INFO_PATH.parent.mkdir(parents=True, exist_ok=True)
    if WORLD_INFO_PATH.exists():
        try:
            shutil.copy2(WORLD_INFO_PATH, WORLD_INFO_PATH.with_name(WORLD_INFO_PATH.name + ".bak"))
        except Exception:
            pass
    tmp = WORLD_INFO_PATH.with_name(WORLD_INFO_PATH.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(WORLD_INFO_PATH)

def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")

class WorldInfoEntry(BaseModel):
    scope: str = ""
    keys: list[str] = []
    content: str = ""
    priority: int = 0
    constant: bool = False
    regex: bool = False
    match_mode: str = "or"
    exclude: list[str] = []
    enabled: bool = True

class WorldInfoGroups(BaseModel):
    agent_groups: dict[str, list[str]] = {}

@app.get("/api/world_info")
async def world_info_list():
    data = _load_world_info()
    entries = data.get("entries", [])
    if not isinstance(entries, list):
        entries = []
    return {"entries": entries, "total": len(entries)}

@app.post("/api/world_info")
async def world_info_add(body: WorldInfoEntry):
    keys = [str(k).strip() for k in body.keys if str(k).strip()]
    if not keys:
        raise HTTPException(400, "至少需要一个触发关键词")
    content = str(body.content or "").strip()
    if not content:
        raise HTTPException(400, "内容不能为空")
    data = _load_world_info()
    entries = data.setdefault("entries", [])
    if not isinstance(entries, list):
        entries = []
        data["entries"] = entries
    now = _now_iso()
    entry_id = f"wi_{int(datetime.now().timestamp() * 1000)}"
    entries.append({
        "id": entry_id,
        "scope": str(body.scope or "").strip() or "main",
        "keys": keys,
        "content": content,
        "priority": int(body.priority or 0),
        "constant": bool(body.constant),
        "regex": bool(body.regex),
        "match_mode": body.match_mode if body.match_mode in ("or", "and") else "or",
        "exclude": [str(x).strip() for x in (body.exclude or []) if str(x).strip()],
        "enabled": bool(body.enabled),
        "created_at": now,
        "updated_at": now,
    })
    _save_world_info(data)
    return {"ok": True, "id": entry_id}

@app.get("/api/world_info/groups")
async def world_info_groups_get():
    if WORLD_INFO_GROUPS_PATH.exists():
        try:
            data = json.loads(WORLD_INFO_GROUPS_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {"agent_groups": {}}

@app.put("/api/world_info/groups")
async def world_info_groups_put(body: WorldInfoGroups):
    WORLD_INFO_GROUPS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = WORLD_INFO_GROUPS_PATH.with_name(WORLD_INFO_GROUPS_PATH.name + ".tmp")
    tmp.write_text(
        json.dumps({"agent_groups": body.agent_groups}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.replace(WORLD_INFO_GROUPS_PATH)
    return {"ok": True}

@app.put("/api/world_info/{entry_id}")
async def world_info_update(entry_id: str, body: WorldInfoEntry):
    keys = [str(k).strip() for k in body.keys if str(k).strip()]
    if not keys:
        raise HTTPException(400, "至少需要一个触发关键词")
    content = str(body.content or "").strip()
    if not content:
        raise HTTPException(400, "内容不能为空")
    data = _load_world_info()
    entries = data.get("entries", [])
    if not isinstance(entries, list):
        raise HTTPException(500, "世界书数据损坏")
    entry = next((e for e in entries if isinstance(e, dict) and e.get("id") == entry_id), None)
    if entry is None:
        raise HTTPException(404, "条目不存在")
    entry["scope"] = str(body.scope or "").strip() or entry.get("scope", "main")
    entry["keys"] = keys
    entry["content"] = content
    entry["priority"] = int(body.priority or 0)
    entry["constant"] = bool(body.constant)
    entry["regex"] = bool(body.regex)
    entry["match_mode"] = body.match_mode if body.match_mode in ("or", "and") else "or"
    entry["exclude"] = [str(x).strip() for x in (body.exclude or []) if str(x).strip()]
    entry["enabled"] = bool(body.enabled)
    entry["updated_at"] = _now_iso()
    _save_world_info(data)
    return {"ok": True}

@app.delete("/api/world_info/{entry_id}")
async def world_info_delete(entry_id: str):
    data = _load_world_info()
    entries = data.get("entries", [])
    if not isinstance(entries, list):
        raise HTTPException(500, "世界书数据损坏")
    entry = next((e for e in entries if isinstance(e, dict) and e.get("id") == entry_id), None)
    if entry is None:
        raise HTTPException(404, "条目不存在")
    entries.remove(entry)
    _save_world_info(data)
    return {"ok": True}


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
    _, tool_summary, tool_json_error = _load_tool_summary()
    if SYSTEM_PROMPT_ROOT.exists():
        # 根目录下的直接文件（全局通用提示词）；.bak 备份不展示，避免误编辑
        for f in sorted(SYSTEM_PROMPT_ROOT.iterdir()):
            if f.is_file() and not f.name.startswith(".") and not f.name.endswith(".bak"):
                global_files.append({
                    "name": f.name,
                    "size": f.stat().st_size,
                    "kind": "tool_catalog" if f.name.lower() == tool_catalog.CATALOG_FILENAME.lower() else "global",
                })
        for d in sorted(SYSTEM_PROMPT_ROOT.iterdir()):
            if d.is_dir() and d.name == "global":
                for f in sorted(d.iterdir()):
                    if f.is_file() and not f.name.startswith(".") and not f.name.endswith(".bak"):
                        global_files.append({"name": f.name, "size": f.stat().st_size, "kind": "global"})
            elif d.is_dir() and not d.name.startswith("."):
                files = []
                for f in sorted(d.iterdir()):
                    if f.is_file() and not f.name.startswith(".") and not f.name.endswith(".bak"):
                        entry = {"name": f.name, "size": f.stat().st_size, "kind": "prompt"}
                        if f.name.lower() == "tool.md":
                            # 选项式（有 tool-inject 块）还是老式手写原文，列表里直接标出来
                            try:
                                options, _ = tool_catalog.parse_tool_md(f.read_text(encoding="utf-8"))
                            except Exception:
                                options = None
                            entry["kind"] = "tool_options" if options is not None else "tool_legacy"
                        files.append(entry)
                agents.append({
                    "name": d.name,
                    "files": files,
                    "declares_tool_md": _tool_md_declared(d.name),
                })
    return {
        "agents": agents,
        "global_files": global_files,
        "tool_catalog": {
            "exists": TOOL_CATALOG_PATH.exists(),
            "json_error": tool_json_error,
            "section_count": tool_summary.get("section_count", 0),
            "tool_count": tool_summary.get("tool_count", 0),
        },
    }

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
    # 优先查找根目录文件，再查找 global/ 子目录
    fp = SYSTEM_PROMPT_ROOT / filename
    if not fp.exists():
        fp = SYSTEM_PROMPT_ROOT / "global" / filename
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
    # agent 为空或 "global" 时写全局文件：优先写回文件原有位置（根目录 > global/ 子目录），
    # 两处都不存在时写根目录（orchestrator 只读根目录的 GLOBAL_SETTING.md / TOOL.json 等）。
    if body.agent and body.agent != "global":
        fp = SYSTEM_PROMPT_ROOT / body.agent / body.filename
    else:
        root_fp = SYSTEM_PROMPT_ROOT / body.filename
        global_fp = SYSTEM_PROMPT_ROOT / "global" / body.filename
        if root_fp.exists():
            fp = root_fp
        elif global_fp.exists():
            fp = global_fp
        else:
            fp = root_fp
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text(body.content, encoding="utf-8")
    # .json（如全局工具目录 TOOL.json）：写法照存，但校验一下并回报，
    # 避免写坏 JSON 后 orchestrator 静默降级、排查困难。
    json_error = ""
    if fp.suffix.lower() == ".json":
        try:
            json.loads(body.content)
        except json.JSONDecodeError as exc:
            json_error = f"JSON 解析失败：{exc}"
    return {"ok": True, "json_error": json_error}

@app.delete("/api/system_prompt/file/{agent}/{filename:path}")
async def delete_sp(agent: str, filename: str):
    # agent == "global" 时删除全局文件：根目录优先，其次 global/ 子目录
    if agent in ("global", ""):
        root_fp = SYSTEM_PROMPT_ROOT / filename
        global_fp = SYSTEM_PROMPT_ROOT / "global" / filename
        if root_fp.exists():
            root_fp.unlink()
            return {"ok": True, "deleted": str(root_fp)}
        if global_fp.exists():
            global_fp.unlink()
            return {"ok": True, "deleted": str(global_fp)}
        raise HTTPException(404, "文件不存在")
    fp = SYSTEM_PROMPT_ROOT / agent / filename
    if not fp.exists():
        raise HTTPException(404, "文件不存在")
    fp.unlink()
    return {"ok": True}


# ═══ 工具目录（TOOL.json）→ 智能体 TOOL.md 勾选生成 ═══════════
# 目录：orchestrator/system_prompt/TOOL.json（全局）
# 智能体侧：orchestrator/system_prompt/<agent>/TOOL.md 只存注入选项 + 正文补充

TOOL_CATALOG_PATH = SYSTEM_PROMPT_ROOT / tool_catalog.CATALOG_FILENAME
AGENT_LIST_PATH = CONFIG_ROOT / "orchestrator" / "config" / "agent_list.json"


def _load_tool_summary() -> tuple[dict, dict, str]:
    """读取 TOOL.json，返回 (catalog, summary, json_error)。"""
    catalog, json_error = tool_catalog.load_catalog(TOOL_CATALOG_PATH)
    return catalog, tool_catalog.summarize_catalog(catalog), json_error


def _tool_md_declared(agent_id: str) -> bool | None:
    """agent_list.json 里该智能体的 files 是否声明了 TOOL.md / TOOL.json。

    返回 None 表示无法判断（配置缺失或损坏）。只做展示提醒，不影响注入。
    """
    try:
        if not AGENT_LIST_PATH.exists():
            return None
        data = json.loads(AGENT_LIST_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None

    found = False

    def _scan(agents) -> None:
        nonlocal found
        if not isinstance(agents, dict):
            return
        entry = agents.get(agent_id)
        if isinstance(entry, dict) and isinstance(entry.get("files"), list):
            for name in entry["files"]:
                if str(name).strip().lower() in ("tool.md", "tool.json"):
                    found = True

    if "default" not in data and "users" not in data:
        _scan(data)  # 旧平铺格式
    else:
        default_cfg = data.get("default") or {}
        _scan(default_cfg.get("agents") if isinstance(default_cfg.get("agents"), dict) else default_cfg)
        users_cfg = data.get("users") or {}
        if isinstance(users_cfg, dict):
            for user_cfg in users_cfg.values():
                if isinstance(user_cfg, dict):
                    _scan(user_cfg.get("agents") if isinstance(user_cfg.get("agents"), dict) else user_cfg)
    return found


@app.get("/api/tool/catalog")
async def tool_catalog_get():
    """全局工具目录的解析结果（段 / 工具 / 分组 / 是否默认注入）。"""
    catalog, summary, json_error = _load_tool_summary()
    return {
        "exists": TOOL_CATALOG_PATH.exists(),
        "path": str(TOOL_CATALOG_PATH.relative_to(CONFIG_ROOT)).replace("\\", "/"),
        "json_error": json_error,
        "summary": summary,
        "groups": tool_catalog.collect_groups(summary),
        "catalog": catalog,
    }


@app.get("/api/tool/options")
async def tool_options_get(agent: str = Query("")):
    """取某智能体 TOOL.md 的当前状态：options（选项式）/ legacy（老式原文）/ missing。"""
    if not agent:
        raise HTTPException(400, "缺少 agent 参数")
    path = SYSTEM_PROMPT_ROOT / agent / "TOOL.md"
    _, summary, _ = _load_tool_summary()
    payload = {
        "agent": agent,
        "declares_tool_md": _tool_md_declared(agent),
        "default_options": tool_catalog.default_options(summary),
    }
    if not path.exists():
        return {**payload, "mode": "missing", "options": None, "body": "", "warnings": []}

    text = path.read_text(encoding="utf-8")
    options, body = tool_catalog.parse_tool_md(text)
    if options is None:
        return {
            **payload,
            "mode": "legacy",
            "options": None,
            "body": body,
            "warnings": ["当前是手写原文式 TOOL.md：没有选项块，整份原文照旧注入"],
        }
    picked_sections, picked_tools = tool_catalog.selection_from_options(summary, options)
    return {
        **payload,
        "mode": "options",
        "options": options,
        "body": body,
        "picked_sections": sorted(picked_sections),
        "picked_tools": sorted(picked_tools),
        "warnings": tool_catalog.validate_options(summary, options),
    }


@app.post("/api/tool/generate")
async def tool_generate(body: ToolGenerateRequest):
    """按勾选生成 TOOL.md 全文 + 注入预览（不落盘，前端确认后再保存）。"""
    catalog, summary, json_error = _load_tool_summary()

    if body.sections is not None:
        # 勾选模式：勾选 → 选项块的规则统一在 tool_catalog.options_from_selection
        options = tool_catalog.options_from_selection(
            summary, set(body.sections or []), set(body.tools or []), body=body.body
        )
        if options.get("enabled") is not False:
            if body.show_title is False:
                options.setdefault("show_title", False)
            if str(body.body_position or "after").strip().lower() == "before":
                options["body_position"] = "before"
    else:
        options = body.options or {}

    return {
        "ok": True,
        "content": tool_catalog.build_tool_md(options, body.body),
        "preview": tool_catalog.build_injection(catalog, options, body.body),
        "options": options,
        "warnings": tool_catalog.validate_options(summary, options),
        "json_error": json_error,
    }


# ═══ System Prompt 分组（仅展示用，不影响存储结构）═══════════
# 存储：orchestrator/system_prompt/.sp_groups.json（点文件，列表/编排器均忽略）
# 结构：{"groups": {"组名": ["agent1", ...]}}

SP_GROUPS_PATH = SYSTEM_PROMPT_ROOT / ".sp_groups.json"

class SpGroups(BaseModel):
    groups: dict[str, list[str]] = {}

def _load_sp_groups() -> dict[str, list[str]]:
    if SP_GROUPS_PATH.exists():
        try:
            data = json.loads(SP_GROUPS_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("groups"), dict):
                return data["groups"]
        except Exception:
            pass
    return {}

@app.get("/api/system_prompt/groups")
async def sp_groups_get():
    return {"groups": _load_sp_groups()}

@app.put("/api/system_prompt/groups")
async def sp_groups_put(body: SpGroups):
    groups = {}
    for name, agents in body.groups.items():
        n = str(name).strip()
        if not n:
            continue
        groups[n] = [str(a).strip() for a in agents if str(a).strip()]
    SP_GROUPS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = SP_GROUPS_PATH.with_name(SP_GROUPS_PATH.name + ".tmp")
    tmp.write_text(json.dumps({"groups": groups}, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(SP_GROUPS_PATH)
    return {"ok": True, "groups": groups}


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


# ═══ QQ 渠道绑定（官方 bot 的 openid ↔ 规范 user_id）══════════
# dashboard 只是转发：真相在 user-service 的 channels 里，
# 官方 bot 侧另有 /id、/bind <QQ号> 命令，两条路径写的是同一份数据。

QQ_OFFICIAL_CHANNEL = os.environ.get("QQ_OFFICIAL_CHANNEL", "qqofficial")
# NFS 上的官方 bot 配置：dashboard 挂载了整个 config 卷，所以可以直接读写
QQ_OFFICIAL_CONFIG = CONFIG_ROOT / "qq-official" / "qq_official_config.json"
QQ_OFFICIAL_DEPLOYMENT = "qq-official-service"


def _load_qq_official_config() -> dict:
    if QQ_OFFICIAL_CONFIG.exists():
        try:
            data = json.loads(QQ_OFFICIAL_CONFIG.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {}


@app.get("/api/qq/app")
async def api_qq_app():
    """返回 AppID 配置状态（**不回传 secret 明文**）。"""
    data = _load_qq_official_config()
    return {
        "ok": True,
        "path": str(QQ_OFFICIAL_CONFIG.relative_to(CONFIG_ROOT)).replace("\\", "/"),
        "app_id": str(data.get("app_id") or ""),
        "secret_set": bool(str(data.get("client_secret") or "").strip()),
        "channel": str(data.get("channel") or QQ_OFFICIAL_CHANNEL),
        "api_base": str(data.get("api_base") or "https://api.bot.qq.com"),
        "configured": bool(str(data.get("app_id") or "").strip())
        and bool(str(data.get("client_secret") or "").strip()),
    }


@app.post("/api/qq/app")
async def api_qq_app_save(payload: dict):
    """保存 AppID/AppSecret（保留文件里的其它键）。

    `client_secret` 留空 = 不改动原有 secret，避免用户只想改 AppID 时把密钥清空。
    """
    data = _load_qq_official_config()
    app_id = str(payload.get("app_id") or "").strip()
    secret = str(payload.get("client_secret") or "").strip()
    if app_id:
        data["app_id"] = app_id
    if secret:
        data["client_secret"] = secret
    if not str(data.get("app_id") or "").strip() or not str(data.get("client_secret") or "").strip():
        raise HTTPException(400, "AppID 与 AppSecret 都必须有值")

    QQ_OFFICIAL_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    tmp = QQ_OFFICIAL_CONFIG.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(QQ_OFFICIAL_CONFIG)
    return {
        "ok": True,
        "configured": True,
        "deployment": QQ_OFFICIAL_DEPLOYMENT,
        "restart_hint": f"POST /api/restart?service={QQ_OFFICIAL_DEPLOYMENT}",
    }


def _user_service_http(path: str, payload: dict | None = None, method: str = "GET") -> dict:
    """调用 user-service 的轻量 HTTP 端点（失败抛 HTTPException，前端能看到原因）。"""
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{USER_SERVICE_URL}{path}",
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            return body if isinstance(body, dict) else {}
    except urllib.error.HTTPError as exc:
        raise HTTPException(exc.code, f"user-service 返回 {exc.code}")
    except Exception as exc:
        raise HTTPException(502, f"user-service 不可达：{exc}")


@app.get("/api/qq/channels")
async def api_qq_channels(channel: str = QQ_OFFICIAL_CHANNEL):
    """列出某渠道的绑定（默认 qqofficial）。"""
    body = _user_service_http(f"/channels/{urllib.parse.quote(channel)}")
    return {"ok": True, "channel": channel, "bindings": body.get("bindings") or []}


@app.post("/api/qq/bind")
async def api_qq_bind(payload: dict):
    """绑定：{user_id, channel_user_id, channel?}。"""
    body = {
        "user_id": str(payload.get("user_id") or "").strip(),
        "channel": str(payload.get("channel") or QQ_OFFICIAL_CHANNEL).strip(),
        "channel_user_id": str(payload.get("channel_user_id") or "").strip(),
        "priority": int(payload.get("priority") or 0),
    }
    if not body["user_id"] or not body["channel_user_id"]:
        raise HTTPException(400, "user_id 与 channel_user_id 都不能为空")
    result = _user_service_http("/channels/bind", body, method="POST")
    return {"ok": bool(result.get("ok")), "message": result.get("message") or ""}


@app.post("/api/qq/unbind")
async def api_qq_unbind(payload: dict):
    """解绑：{user_id, channel?}。"""
    body = {
        "user_id": str(payload.get("user_id") or "").strip(),
        "channel": str(payload.get("channel") or QQ_OFFICIAL_CHANNEL).strip(),
    }
    if not body["user_id"]:
        raise HTTPException(400, "user_id 不能为空")
    result = _user_service_http("/channels/unbind", body, method="POST")
    return {"ok": bool(result.get("ok")), "message": result.get("message") or ""}


# ═══ Session 管理（按 openviking 用户隔离）═══════════════════

def _user_root() -> Path:
    """openviking 用户根目录；兼容旧配置 SESSION_ROOT 指向某个用户的 sessions 目录。"""
    root = SESSION_ROOT
    if root.name == "sessions":
        root = root.parent.parent
    return root

def _user_sessions_dir(user_id: str) -> Path:
    return _user_root() / user_id / "sessions"

@app.get("/api/session/users")
async def session_users():
    """列出所有 openviking 用户（含各自 session 数量）"""
    root = _user_root()
    if not root.exists():
        return {"users": []}
    users = []
    for d in sorted(root.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not d.is_dir() or d.name.startswith("."):
            continue
        sessions_dir = d / "sessions"
        count = 0
        if sessions_dir.is_dir():
            count = sum(1 for s in sessions_dir.iterdir() if s.is_dir() and not s.name.startswith("."))
        users.append({"user_id": d.name, "session_count": count})
    return {"users": users}

@app.get("/api/session/sessions")
async def session_list(user: str = Query("")):
    """列出指定用户的所有 session 文件夹"""
    if not user:
        raise HTTPException(400, "缺少 user 参数")
    sessions_root = _user_sessions_dir(user)
    if not sessions_root.exists():
        return {"user": user, "sessions": []}
    sessions = []
    for d in sorted(sessions_root.iterdir()):
        if not d.is_dir() or d.name.startswith("."):
            continue
        msg_file = d / "messages.jsonl"
        msg_count = 0
        if msg_file.exists():
            with open(msg_file, "r", encoding="utf-8") as f:
                msg_count = sum(1 for _ in f)
        has_history = (d / "history").exists()
        sessions.append({
            "name": d.name,
            "msg_count": msg_count,
            "has_history": has_history,
        })
    return {"user": user, "sessions": sessions}

@app.get("/api/session/{user}/{name}/messages")
async def session_messages(user: str, name: str):
    msg_file = _user_sessions_dir(user) / name / "messages.jsonl"
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

@app.delete("/api/session/{user}/{name}/messages")
async def session_delete_messages(user: str, name: str, body: MessageDelete):
    msg_file = _user_sessions_dir(user) / name / "messages.jsonl"
    if not msg_file.exists():
        raise HTTPException(404, "messages.jsonl 不存在")
    lines_to_delete = set(body.lines)
    with open(msg_file, "r", encoding="utf-8") as f:
        all_lines = f.readlines()
    kept = [l for i, l in enumerate(all_lines) if i not in lines_to_delete]
    with open(msg_file, "w", encoding="utf-8") as f:
        f.writelines(kept)
    return {"ok": True, "deleted": len(lines_to_delete & set(range(len(all_lines)))), "remaining": len(kept)}

@app.delete("/api/session/{user}/{name}")
async def session_delete(user: str, name: str):
    session_dir = _user_sessions_dir(user) / name
    if not session_dir.exists():
        raise HTTPException(404, "Session 不存在")
    shutil.rmtree(session_dir)
    return {"ok": True, "deleted": str(session_dir)}


# ═══ 长期记忆查询（独立功能，使用 OpenViking 真实语义搜索）═══

def _openviking_user_key(user_id: str) -> str:
    """获取用户的 OpenViking per-user key：优先 user-service，回退 agent-service key。"""
    try:
        req = urllib.request.Request(f"{USER_SERVICE_URL}/openviking_key/{user_id}", method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if data.get("ok") and data.get("api_key"):
                return data["api_key"]
    except Exception:
        pass
    if OPENVIKING_API_KEY_FILE.exists():
        return OPENVIKING_API_KEY_FILE.read_text(encoding="utf-8").strip()
    return ""

@app.get("/api/memory/search")
async def memory_search(user: str = Query(""), query: str = Query(""), limit: int = Query(20)):
    """调用 OpenViking /api/v1/search/find 做长期记忆语义搜索（用户级，与 session 无关）。"""
    if not user:
        raise HTTPException(400, "缺少 user 参数")
    if not query.strip():
        return {"hits": [], "total": 0}
    api_key = _openviking_user_key(user)
    if not api_key:
        raise HTTPException(400, f"无法获取用户 {user} 的 OpenViking key")
    body = json.dumps({
        "query": query,
        "target_uri": f"viking://user/{user}",
        "limit": max(1, min(limit, 100)),
        "context_type": "memory",
    }).encode("utf-8")
    req = urllib.request.Request(f"{OPENVIKING_SERVER_URL}/api/v1/search/find", data=body, method="POST")
    req.add_header("X-API-Key", api_key)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:200]
        raise HTTPException(502, f"OpenViking 搜索失败 HTTP {e.code}: {detail}")
    except Exception as e:
        raise HTTPException(502, f"OpenViking 搜索失败: {e}")
    result = data.get("result", {}) or {}
    memories = result.get("memories", []) or []
    hits = []
    for m in memories:
        if not isinstance(m, dict):
            continue
        hits.append({
            "content": m.get("content", ""),
            "score": m.get("score", 0.0),
            "uri": m.get("uri", ""),
            "session": m.get("session_id", ""),
        })
    return {"hits": hits, "total": result.get("total", len(hits))}


# ═══ 长期事件（PROCESS）管理 ═════════════════════════════════
# 运行文件：orchestrator/config/process/<user_id>/<agent_id>.json
# 模板：    orchestrator/config/process/process_templates#/<agent_id>.json
#（process_templates# 目录名含 '#'，user_id 经 _safe_segment 生成不了含 '#' 的段名，
#  防止"邪门用户"占用模板目录；dashboard 直读直写，process-init 工具读模板载入）

PROCESS_ROOT = CONFIG_ROOT / "orchestrator" / "config" / "process"
PROCESS_TPL_DIR = PROCESS_ROOT / "process_templates#"

class ProcessFile(BaseModel):
    content: str

def _process_users() -> list[str]:
    if not PROCESS_ROOT.exists():
        return []
    return sorted(d.name for d in PROCESS_ROOT.iterdir()
                  if d.is_dir() and not d.name.startswith(".") and d.name != "process_templates#")

def _process_agents(user_id: str) -> list[str]:
    d = PROCESS_ROOT / user_id
    if not d.exists():
        return []
    return sorted(f.stem for f in d.iterdir() if f.is_file() and f.suffix == ".json")

def _read_json_text(path: Path) -> str:
    if not path.exists():
        raise HTTPException(404, "文件不存在")
    return path.read_text(encoding="utf-8")

def _write_json_text(path: Path, content: str) -> None:
    # 校验合法 JSON，避免写入损坏数据
    try:
        data = json.loads(content)
    except json.JSONDecodeError as e:
        raise HTTPException(400, f"JSON 解析失败：{e}")
    if not isinstance(data, dict):
        raise HTTPException(400, "JSON 顶层必须是对象")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            shutil.copy2(path, path.with_name(path.name + ".bak"))
        except Exception:
            pass
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)

@app.get("/api/process/users")
async def process_users_list():
    return {"users": _process_users()}

@app.get("/api/process/agents")
async def process_agents_list(user: str = Query("")):
    if not user:
        raise HTTPException(400, "缺少 user 参数")
    return {"user": user, "agents": _process_agents(user)}

@app.get("/api/process/file")
async def process_file_get(user: str = Query(""), agent: str = Query("")):
    if not user or not agent:
        raise HTTPException(400, "缺少 user/agent 参数")
    path = PROCESS_ROOT / user / f"{agent}.json"
    return {"user": user, "agent": agent, "content": _read_json_text(path)}

@app.put("/api/process/file")
async def process_file_put(user: str = Query(""), agent: str = Query(""), body: ProcessFile | None = None):
    if not user or not agent:
        raise HTTPException(400, "缺少 user/agent 参数")
    content = body.content if body is not None else ""
    path = PROCESS_ROOT / user / f"{agent}.json"
    _write_json_text(path, content)
    return {"ok": True, "path": str(path.relative_to(CONFIG_ROOT)).replace("\\", "/")}

@app.get("/api/process/template")
async def process_template_get(agent: str = Query("")):
    if not agent:
        raise HTTPException(400, "缺少 agent 参数")
    path = PROCESS_TPL_DIR / f"{agent}.json"
    if not path.exists():
        return {"agent": agent, "exists": False, "content": "{\n  \"turn\": 0,\n  \"items\": [],\n  \"vars\": {},\n  \"aliases\": {}\n}"}
    return {"agent": agent, "exists": True, "content": _read_json_text(path)}

@app.put("/api/process/template")
async def process_template_put(agent: str = Query(""), body: ProcessFile | None = None):
    if not agent:
        raise HTTPException(400, "缺少 agent 参数")
    content = body.content if body is not None else ""
    path = PROCESS_TPL_DIR / f"{agent}.json"
    _write_json_text(path, content)
    return {"ok": True, "path": str(path.relative_to(CONFIG_ROOT)).replace("\\", "/")}


# ─── 静态页面 ────────────────────────────────────────────────
# 页面骨架在 static/index.html，各标签页的标记在 static/assets/tabs/<tab>.html，
# 由 index.html 里的 `<!-- @include: tabs/xxx.html -->` 占位在返回时拼进去。
# 手写 include（不引模板引擎依赖）：分片按上方标签页拆分，改了不用重启服务。

_INCLUDE_RE = re.compile(r"^([ \t]*)<!--\s*@include:\s*([0-9A-Za-z_./-]+)\s*-->[ \t]*$", re.MULTILINE)


def _render_index() -> str:
    """读取 index.html 并把 @include 占位替换成标签页分片内容。

    分片路径相对 `static/assets/`（如 `tabs/pods.html`）；越界路径一律拒绝。
    """
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    root = ASSETS_DIR.resolve()

    def _replace(match: re.Match) -> str:
        indent, name = match.group(1), match.group(2)
        path = (ASSETS_DIR / name).resolve()
        # 只允许取 assets 目录内的分片，避免 @include 变成任意文件读取
        if not str(path).startswith(str(root)) or not path.is_file():
            return f"{indent}<!-- @include 分片不存在: {name} -->"
        lines = path.read_text(encoding="utf-8").rstrip("\n").splitlines()
        return "\n".join(indent + line if line.strip() else line for line in lines)

    return _INCLUDE_RE.sub(_replace, html)


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    if (STATIC_DIR / "index.html").exists():
        return _render_index()
    return "<h1>Dashboard</h1><p>index.html not found</p>"


# ─── 入口 ────────────────────────────────────────────────────

def main():
    uvicorn.run("app.main:app", host="0.0.0.0", port=5700, reload=False)

if __name__ == "__main__":
    main()

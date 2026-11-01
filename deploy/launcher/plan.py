"""单体模式的启动计划：功能选择 + 环境 → 要起哪些服务、用哪些端口、注入哪些 env、建哪些目录。

纯函数，不启动任何进程，便于离线自测（launcher_test.py）。
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .services import FEATURE_SERVICES, SERVICES, ServiceSpec

# features.json 的功能里，模式选择决定要启动哪些服务
MODE_SERVICES = {
    "memory": {
        "local": [],
        "openviking": ["openviking-context-service"],
        "openviking+install": ["openviking-context-service", "embedding-service"],
    },
    "qq": {
        "satori": ["qq-llbot-service"],
        "onebot": ["qq-onebot-service"],
    },
}

DATA_SUBDIRS = [
    "config",
    "workspace",
    "workspace/skills",
    "assets",
    "assets/images",
    "images",
    "users",
    "session",
    "timer-tasks",
    "logs",
]


@dataclass
class Plan:
    data_dir: Path
    repo_root: Path
    features: list[str]
    modes: dict[str, str]
    services: list[ServiceSpec]
    env: dict[str, str]
    dirs: list[Path] = field(default_factory=list)
    web_port: int = 0            # 单体前端端口（0 = 不起前端）
    assets_port: int = 0         # 单体图床端口（0 = 不起图床）
    gateway_port: int = 0        # /api 反代的上游
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "data_dir": str(self.data_dir),
            "repo_root": str(self.repo_root),
            "features": self.features,
            "modes": self.modes,
            "services": [
                {
                    "id": svc.id,
                    "dir": svc.dir_name,
                    "cmd": list(svc.cmd),
                    "ports": svc.ports,
                    "health": svc.health,
                }
                for svc in self.services
            ],
            "env": self.env,
            "dirs": [str(item) for item in self.dirs],
            "web": {"port": self.web_port, "upstream": f"http://127.0.0.1:{self.gateway_port}"} if self.web_port else {},
            "assets": {"port": self.assets_port} if self.assets_port else {},
            "warnings": self.warnings,
        }


def _feature_service_list(feature: str, modes: dict[str, str]) -> list[str]:
    """单个功能需要哪些服务（含模式展开）。"""
    if feature in MODE_SERVICES:
        mode = modes.get(feature, "")
        table = MODE_SERVICES[feature]
        key = mode
        if feature == "memory" and mode == "openviking" and modes.get("memory.source") == "install":
            key = "openviking+install"
        return list(table.get(key, []))
    return list(FEATURE_SERVICES.get(feature, []))



LAUNCHER_SETTINGS_PATH = Path("config") / "launcher.json"


def load_settings(data_dir: Path) -> dict:
    """读 <DATA_DIR>/config/launcher.json（面板/用户改这里，不用设环境变量）。

    文件不存在或坏掉都返回空 dict，并由调用方给出显式告警 —— 不做静默猜测。
    """
    path = Path(data_dir) / LAUNCHER_SETTINGS_PATH
    if not path.is_file():
        return {}
    try:
        # Windows 记事本默认存带 BOM 的 UTF-8，用 utf-8-sig 兼容（否则用户手改完就报错）
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"配置文件写坏了：{path}（{exc}）") from exc


def openviking_settings(data_dir: Path) -> dict:
    """取 launcher.json 里的 openviking 段（带默认值）。"""
    try:
        data = load_settings(data_dir)
    except ValueError as exc:
        return {"_error": str(exc)}
    section = data.get("openviking") or {}
    return section if isinstance(section, dict) else {}



def clawhub_settings(data_dir: Path) -> dict:
    """launcher.json 的 clawhub 段：bin / node_dir / skill_root / enabled。"""
    try:
        data = load_settings(data_dir)
    except ValueError as exc:
        return {"_error": str(exc)}
    section = data.get("clawhub") or {}
    return section if isinstance(section, dict) else {}


def find_node_dir() -> str:
    """找 Node 安装目录（clawhub.cmd 内部要调 node，PATH 里必须有它）。

    顺序：当前 PATH → 常见安装位置。返回空串表示没装。
    """
    exe = shutil.which("node")
    if exe:
        return str(Path(exe).parent)
    candidates = [
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "nodejs",
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "nodejs",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "nodejs",
        Path(os.environ.get("APPDATA", "")) / "npm",
    ]
    for item in candidates:
        if item and (item / "node.exe").is_file():
            return str(item)
    return ""


def find_clawhub_bin(node_dir: str = "") -> str:
    """找 clawhub 可执行文件（npm 全局安装，常在 %APPDATA%\\npm）。"""
    for name in ("clawhub.cmd", "clawhub.exe", "clawhub.ps1"):
        found = shutil.which(name)
        if found:
            return found
    candidates = [
        Path(os.environ.get("APPDATA", "")) / "npm" / "clawhub.cmd",
        Path(os.environ.get("APPDATA", "")) / "npm" / "clawhub.exe",
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "nodejs" / "clawhub.cmd",
    ]
    for item in candidates:
        if item.is_file():
            return str(item)
    return ""



def qq_settings(data_dir: Path) -> dict:
    """launcher.json 的 qq 段：mode / ws_url / token / self_id / group_require_at。"""
    try:
        data = load_settings(data_dir)
    except ValueError as exc:
        return {"_error": str(exc)}
    section = data.get("qq") or {}
    return section if isinstance(section, dict) else {}


def build(
    repo_root: Path,
    data_dir: Path,
    features: list[str],
    modes: dict[str, str] | None = None,
    extra_services: list[str] | None = None,
) -> Plan:
    """构造启动计划。

    `features` 用 features.json 的 id（如 core / memory / tools.basic / qq）；
    `modes` 例如 {"memory": "openviking", "memory.source": "install", "qq": "satori"}。
    """
    modes = dict(modes or {})
    wanted: list[str] = []

    for feature in features:
        for service_id in _feature_service_list(feature, modes):
            if service_id and service_id not in wanted:
                wanted.append(service_id)
    for service_id in extra_services or []:
        if service_id not in wanted:
            wanted.append(service_id)

    unknown = [item for item in wanted if item not in SERVICES]
    if unknown:
        raise ValueError(f"未知服务：{unknown}")

    services = [SERVICES[item] for item in wanted]

    # 端口冲突检查（单体全跑在一台机器上）
    used: dict[int, str] = {}
    warnings: list[str] = []
    for svc in services:
        for name, port in svc.ports.items():
            if port in used:
                raise ValueError(f"端口冲突：{svc.id}.{name}={port} 已被 {used[port]} 占用")
            used[port] = f"{svc.id}.{name}"

    env = {
        "DATA_DIR": str(data_dir),
        "CAPABILITY_PATH": str(Path(data_dir) / "capability.json"),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
    }

    # 端点全部指向本机（按实际选中的服务判断可用性）
    ids = {svc.id for svc in services}

    def endpoint(service_id: str, port_name: str = "grpc") -> str | None:
        svc = SERVICES[service_id]
        port = svc.ports.get(port_name)
        return f"127.0.0.1:{port}" if port else None

    if "model-proxy-service" in ids:
        env["MODEL_PROXY_TARGET"] = endpoint("model-proxy-service") or ""
    else:
        warnings.append("未包含 model-proxy-service：对话不可用")
    if "user-service" in ids:
        env["USER_SERVICE_TARGET"] = endpoint("user-service") or ""
        env["USER_SERVICE_URL"] = f"http://127.0.0.1:{SERVICES['user-service'].ports['http']}"
    if "task-scheduler-service" in ids:
        env["SCHEDULER_TARGET"] = endpoint("task-scheduler-service") or ""
        env["SCHEDULER_GRPC_TARGET"] = endpoint("task-scheduler-service") or ""
    if "agent-orchestrator-service" in ids:
        env["ORCHESTRATOR_TARGET"] = endpoint("agent-orchestrator-service") or ""
    if "tool-runtime-service" in ids:
        env["TOOL_RUNTIME_TARGET"] = endpoint("tool-runtime-service") or ""
        env["TOOL_RUNTIME_GRPC_TARGET"] = env["TOOL_RUNTIME_TARGET"]
    if "timer-task-service" in ids:
        env["TIMER_TASK_TARGET"] = endpoint("timer-task-service") or ""
    if "openviking-context-service" in ids:
        env["OPENVIKING_CONTEXT_TARGET"] = endpoint("openviking-context-service") or ""

        # openviking 服务器地址来自 <DATA_DIR>/config/launcher.json，命令行/环境变量可覆盖。
        # 没配就一直用文件模式（不写库、不检索），并把原因写进告警与 capability.json，
        # 而不是等用户在服务日志里翻"退化为 mock"。
        ov = openviking_settings(data_dir)
        if ov.get("_error"):
            warnings.append(ov["_error"])
        server_url = str(os.getenv("OPENVIKING_SERVER_URL") or ov.get("server_url") or "").strip()
        if server_url:
            env["OPENVIKING_SERVER_URL"] = server_url
            env["OPENVIKING_ACCOUNT"] = str(ov.get("account") or "my-agent")
            env["OPENVIKING_ACCOUNT_MODE"] = str(ov.get("account_mode") or "fixed")
            if ov.get("file_fallback") is not None:
                env["OPENVIKING_FILE_FALLBACK"] = "true" if ov.get("file_fallback") else "false"
        else:
            # 显式降级：记忆用本地文件，不假装连着库
            env["OPENVIKING_BACKEND"] = "file"
            env["OPENVIKING_FILE_FALLBACK"] = "true"
            warnings.append(
                "openviking 模式已启用但未配置服务器地址：记忆降级为文件模式"
                "（不写库、不检索）。在 单体配置 → config/launcher.json 的 "
                "openviking.server_url 填写，或在启动命令加 --openviking-url"
            )
        # 默认值是 /app/viking_data，Windows 上会变成 C:\app\viking_data（跑到数据目录外）
        env["VIKING_DATA_DIR"] = str(Path(data_dir) / "viking_data")
        cfg_orch = Path(data_dir) / "config" / "orchestrator" / "config"
        env["WORLD_INFO_PATH"] = str(cfg_orch / "world_info" / "world_info.json")
        env["WORLD_INFO_GROUPS_PATH"] = str(cfg_orch / "world_info" / "groups.json")
    if "tool-runtime-service" in ids and "skills" in features:
        # 技能（ClawHub）：单体默认走本机 CLI。clawhub.cmd 内部调 node，
        # 所以把 node 目录拼进 PATH —— 面板/命令行启动时 PATH 可能还是装 Node 之前的老值。
        claw = clawhub_settings(data_dir)
        if claw.get("_error"):
            warnings.append(claw["_error"])
        node_dir = str(os.getenv("MYAGENT_NODE_DIR") or claw.get("node_dir") or find_node_dir()).strip()
        claw_bin = str(os.getenv("MYAGENT_CLAWHUB_BIN") or claw.get("bin") or find_clawhub_bin(node_dir)).strip()
        mode = str(claw.get("mode") or "local").strip().lower()
        skill_root = str(Path(data_dir) / "workspace" / "skills")

        env["CLAW_DOWNLOAD_MODE"] = "local" if mode in ("local", "clawhub") else mode
        env["CLAW_EXTERNAL_VM_SKILL_ROOT_DIR"] = skill_root
        if claw_bin:
            env["CLAW_EXTERNAL_VM_CLAWHUB_BIN"] = claw_bin
        if node_dir:
            env["PATH"] = node_dir + os.pathsep + env.get("PATH", os.environ.get("PATH", ""))
        # 缺东西不静默：明确告诉用户技能只能读
        if not node_dir:
            env["ENABLE_CLAWHUB"] = "false"
            warnings.append(
                "未找到 Node.js：ClawHub 搜索/安装不可用（技能只能读）。"
                "装 Node LTS 后 npm i -g clawhub，或在 launcher.json 的 clawhub.node_dir 指明路径"
            )
        elif not claw_bin:
            env["ENABLE_CLAWHUB"] = "false"
            warnings.append(
                "未找到 clawhub 命令：ClawHub 搜索/安装不可用（技能只能读）。"
                "执行 npm i -g clawhub，或在 launcher.json 的 clawhub.bin 指明 clawhub.cmd 路径"
            )

    qq_ids = {"qq-llbot-service", "qq-onebot-service", "qq-official-service"} & ids
    if qq_ids:
        qq = qq_settings(data_dir)
        if qq.get("_error"):
            warnings.append(qq["_error"])
        mode = str(qq.get("mode") or modes.get("qq") or ("onebot" if "qq-onebot-service" in ids else "satori")).strip()
        if "qq-onebot-service" in ids:
            # NapCat / LLOneBot / go-cqhttp 都兼容 OneBot 11
            env["ONEBOT_MODE"] = str(qq.get("onebot_mode") or "forward").strip().lower()
            ws_url = str(os.getenv("ONEBOT_WS_URL") or qq.get("ws_url") or "").strip()
            if ws_url:
                env["ONEBOT_WS_URL"] = ws_url
            else:
                warnings.append(
                    "未配置 qq.ws_url：OneBot 适配器将尝试连 ws://127.0.0.1:3001/（NapCat 默认 WS 端口）。"
                    "在 launcher.json 的 qq.ws_url 填写 NapCat 的正向 WS 地址"
                )
            listen = str(qq.get("ws_listen") or "").strip()
            if listen:
                env["ONEBOT_WS_LISTEN"] = listen
            token = str(qq.get("token") or "").strip()
            if token:
                env["ONEBOT_TOKEN"] = token
            self_id = str(qq.get("self_id") or "").strip()
            if self_id:
                env["ONEBOT_SELF_ID"] = self_id
            if qq.get("group_require_at") is not None:
                env["ONEBOT_GROUP_REQUIRE_AT"] = "true" if qq.get("group_require_at") else "false"
        if mode:
            warnings.append(f"QQ 适配器：{mode}（多适配器同时在线时按优先级 satori=100 > onebot=90 > official=50 投递）")

    if "embedding-service" in ids:
        env["EMBEDDING_SERVICE_TARGET"] = endpoint("embedding-service") or ""

    if "tool-runtime-service" not in ids:
        warnings.append("未包含 tool-runtime-service：工具调用关闭（capability tool_runtime=false）")

    # 配置路径全部指向 DATA_DIR（服务内不再放配置；不注入就会读到服务内空目录）
    cfg = Path(data_dir) / "config"
    env.update({
        "AGENT_CONFIG_PATH": str(cfg / "orchestrator" / "config" / "agent_list.json"),
        "SYSTEM_PROMPT_DIR": str(cfg / "orchestrator" / "system_prompt"),
        "CONFIG_DIR": str(cfg / "orchestrator" / "config"),
        # tool-runtime 默认读 /app/system_prompts/...（容器路径）；单体下必须指到数据目录，
        # 否则工具权限/杂项设置读了另一份默认值（表现：页面上的名单像被清空）
        "SYSTEM_SETTINGS_PATH": str(cfg / "orchestrator" / "config" / "system_settings.json"),
        "PROCESS_DIR": str(cfg / "orchestrator" / "config" / "process"),
        "WORLD_INFO_DIR": str(cfg / "orchestrator" / "config" / "world_info"),
        "REPLY_IMAGES_DIR": str(cfg / "orchestrator" / "config" / ".reply_images"),
        "MODEL_LIST_PATH": str(cfg / "model-proxy" / "config" / "model_list.json"),
        "MODEL_PROFILES_PATH": str(cfg / "model-proxy" / "config" / "model_profiles.json"),
        "WORKSPACE_DIR": str(Path(data_dir) / "workspace"),
        "SKILL_ROOT_DIR": str(Path(data_dir) / "workspace" / "skills"),
        "IMAGE_ASSET_DIR": str(Path(data_dir) / "assets" / "images"),
        "QQ_LLBOT_CONFIG_PATH": str(cfg / "qq-llbot" / "qq_llbot_config.json"),
        "QQ_OFFICIAL_CONFIG_PATH": str(cfg / "qq-official" / "qq_official_config.json"),
        "OPENVIKING_API_KEY": str(cfg / "openviking" / "api_key"),
        "OPENVIKING_ROOT_API_KEY": str(cfg / "openviking" / "root_api_key"),
        "CONFIG_ROOT": str(cfg),
        # 工具目录（TOOL.json 管"给不给用"）：单体里就是数据目录下的那份
        "TOOL_CATALOG_PATH": str(cfg / "orchestrator" / "system_prompt" / "TOOL.json"),
        # 环境差异化由部署端注入：单体 = 本机（盘符即本地、clawhub 本机 CLI）
        "TOOL_PROVIDERS": "file-copy=local,file-move=local,clawhub=local",
    })

    # 本地模型（model.local / Ollama）：单体里它是宿主进程，不由 launcher 启动，
    # 但把地址透出去，便于 tool-runtime 等组件在需要时直连。
    if "model.local" in features:
        env.setdefault("OLLAMA_BASE_URL", "http://127.0.0.1:11434")

    web_port = 0
    gateway_port = 0
    gateway_spec = SERVICES.get("gateway-backend-service")
    if gateway_spec and "gateway-backend-service" in ids:
        gateway_port = gateway_spec.ports.get("http", 0)
        try:
            web_port = int(os.getenv("WEB_PORT") or 8080)
        except ValueError:
            web_port = 8080
        env["GATEWAY_UPSTREAM"] = f"http://127.0.0.1:{gateway_port}"

    # ── 单机模式地址收口：把 k8s Service DNS 换成 127.0.0.1 ──
    assets_port = int(os.getenv("ASSETS_PORT") or 5102)
    assets_base = f"http://127.0.0.1:{assets_port}"
    dashboard_port = SERVICES["dashboard-service"].ports.get("http", 5700) if "dashboard-service" in ids else 0
    user_http = SERVICES["user-service"].ports.get("http", 5204) if "user-service" in ids else 0

    # 统一存储前缀：所有服务据此推导 workspace/assets 等路径，避免前缀不一致
    # 没有 tool-runtime 时，gateway 用自己的本地实现处理工作空间读写
    env["GATEWAY_WORKSPACE_LOCAL"] = "0" if "tool-runtime-service" in ids else "1"
    env["MYAGENT_STORAGE_ROOT"] = str(data_dir)
    env["WORKSPACE_USERS_BASE"] = str(Path(data_dir) / "workspace" / "users")
    # gateway 的 /api/assets/<file> 与单体图床读同一个目录
    env["ASSETS_DIR"] = str(Path(data_dir) / "assets" / "images")
    env["ASSETS_PORT"] = str(assets_port)
    env["IMAGE_ASSET_DIR"] = str(Path(data_dir) / "assets" / "images")
    # gateway：管理面反代的目标改成本机；且不要在 gateway 进程里再监听 5700（dashboard 自己占）
    env["GATEWAY_PASSTHROUGH_PORTS"] = ""
    if dashboard_port:
        env["ADMIN_DASHBOARD_TARGET"] = f"http://127.0.0.1:{dashboard_port}"
    if user_http:
        env["ADMIN_USER_TARGET"] = f"http://127.0.0.1:{user_http}"
    env["ADMIN_ASSETS_TARGET"] = assets_base
    # dashboard：表情预览按 <base>/assets/<file> 拼
    env["IMAGE_ASSETS_BASE"] = assets_base
    # tool-runtime：image-url / send-image / 表情绑定都用这个前缀
    # gateway 的 /api/assets/<file> 从 ASSETS_DIR 读；单体里就是本地图床目录
    env["ASSETS_DIR"] = str(Path(data_dir) / "assets" / "images")
    env["IMAGE_BASE_URL"] = f"{assets_base}/assets"
    env["REPLY_IMAGE_BASE_URL"] = f"{assets_base}/assets"

    # 记忆后端：单体默认本地；只有 memory 用 openviking 且真的会起该服务时才给地址
    memory_mode = (modes or {}).get("memory", "local") if isinstance(modes, dict) else "local"
    if "openviking-context-service" in ids:
        env.setdefault("OPENVIKING_CONTEXT_TARGET", f"127.0.0.1:{SERVICES['openviking-context-service'].ports.get('grpc', 5301)}")
    else:
        env["OPENVIKING_CONTEXT_TARGET"] = ""
    # dashboard 在单体里没法 kubectl：日志与进程从本地目录读
    env["DASHBOARD_LOG_DIR"] = str(Path(data_dir) / "logs")
    env["DASHBOARD_PIDS_PATH"] = str(Path(data_dir) / "pids.json")

    dirs = [data_dir / item for item in DATA_SUBDIRS]
    return Plan(
        data_dir=Path(data_dir),
        repo_root=Path(repo_root),
        features=list(features),
        modes=modes,
        services=services,
        env=env,
        dirs=dirs,
        warnings=warnings,
        web_port=web_port,
        gateway_port=gateway_port,
        assets_port=assets_port,
    )



CONFIG_SOURCE = "config"          # 仓库里的"最简模板"目录（只复制、不改）

# 模板 → 数据目录的落地规则：保持相对结构（config/orchestrator/... → DATA_DIR/config/orchestrator/...）
CONFIG_SKIP_DIRS = {".git", "__pycache__", ".reply_images"}


def init_config(repo_root: Path, data_dir: Path) -> tuple[int, list[str]]:
    """把仓库 config/ 模板复制到 <DATA_DIR>/config（**不覆盖已有文件**）。

    返回 (新增文件数, 已有文件数)。模板只复制，从不回写仓库。
    """

    src = Path(repo_root) / CONFIG_SOURCE
    dst = Path(data_dir) / "config"
    added, kept = 0, 0
    if not src.is_dir():
        return 0, 0
    for path in sorted(src.rglob("*")):
        rel = path.relative_to(src)
        if any(part in CONFIG_SKIP_DIRS for part in rel.parts):
            continue
        target = dst / rel
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            kept += 1
            continue
        try:
            shutil.copy2(path, target)
            added += 1
        except Exception:  # noqa: BLE001
            continue
    return added, kept


def write_capability(plan: Plan, path: Path) -> None:
    """按计划写一份最小 capability.json（可由 probe_env.py 覆盖成探测版）。"""
    ids = {svc.id for svc in plan.services}
    data = {
        "schema": 1,
        "runtime_tier": "T0",
        "os": "windows",
        "mode": "standalone",
        "features": {
            "tool_runtime": "tool-runtime-service" in ids,
            "memory_mode": "openviking" if "openviking-context-service" in ids else "local",
            "openviking": "openviking-context-service" in ids,
            "clawhub": "skills" in plan.features,
            "browser": "browser" in plan.features,
            "search": "builtin",
            "external_exec": "external-exec" in plan.features,
            "host_files": "hostfiles" in plan.features,
            "image_gen": True,
            "fetch_extract": "full",
            "qq_adapter": "satori" if "qq-llbot-service" in ids else "none",
            "lan_expose": False,
            "storage": "local-dir",
        },
        "degraded": [
            {"feature": "container", "reason": "单体模式（未使用 Docker/k8s）"},
        ],
    }
    # 启动期的降级原因也写进去（面板/网页「杂项」顶部会显示）：
    # 例：选了 openviking 记忆但没填服务器地址 → 记忆实际走文件模式。
    for item in getattr(plan, "warnings", []) or []:
        data["degraded"].append({"feature": "launcher", "reason": str(item)})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

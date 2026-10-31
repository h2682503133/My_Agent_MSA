"""单体模式的启动计划：功能选择 + 环境 → 要起哪些服务、用哪些端口、注入哪些 env、建哪些目录。

纯函数，不启动任何进程，便于离线自测（launcher_test.py）。
"""

from __future__ import annotations

import json
import os
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
        "onebot": [],   # S5：OneBot 适配器
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
        # 默认值是 /app/viking_data，Windows 上会变成 C:\app\viking_data（跑到数据目录外）
        env["VIKING_DATA_DIR"] = str(Path(data_dir) / "viking_data")
        cfg_orch = Path(data_dir) / "config" / "orchestrator" / "config"
        env["WORLD_INFO_PATH"] = str(cfg_orch / "world_info" / "world_info.json")
        env["WORLD_INFO_GROUPS_PATH"] = str(cfg_orch / "world_info" / "groups.json")
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
    import shutil

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
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

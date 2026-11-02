"""单体模式（T0/T3，无 Docker/无 k8s）的本地服务表。

每个服务在单体里都是**独立的本地子进程**（不共享解释器：12 个服务的顶层包都叫 `app`），
launcher 负责按功能启停、注入 127.0.0.1 端点、收集日志。

端口说明：容器里 embedding 与 tool-runtime 都用 5303（不同 Pod 不冲突），
单体都跑在同一台机器上，所以这里给 embedding 换成 5305。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ServiceSpec:
    id: str
    dir_name: str          # services/ 下的目录名
    cmd: tuple[str, ...]   # 相对服务目录的启动命令（python 已由 launcher 提供）
    ports: dict[str, int]  # 名字 → 本机端口（唯一）
    env: dict[str, str] = field(default_factory=dict)
    health: str = "tcp"    # tcp / http
    needs: tuple[str, ...] = ()   # 必须同时运行的服务（仅提示，不强制）
    venv: str = ""         # 非空 = 用 <数据目录>/venvs/<名字> 里的独立解释器/入口脚本
    cwd: str = "service"   # service = 在 services/<dir_name> 下跑；data = 在数据目录下跑


SERVICES: dict[str, ServiceSpec] = {
    # 长期记忆的后端本体（可选）。单体默认不跑：只有「记忆模式=openviking 且来源=本机自建」
    # 时才会被 plan 纳入。它依赖集合与共享 venv 冲突（fastapi>=0.128），所以放在独立 venv 里。
    "openviking": ServiceSpec(
        id="openviking",
        dir_name="",
        cmd=("openviking-server",),
        ports={"http": 1933},
        env={},
        health="http",
        venv="openviking",
        cwd="data",
    ),
    "user-service": ServiceSpec(
        id="user-service",
        dir_name="user-service",
        cmd=("-m", "app.main"),
        ports={"grpc": 5104, "http": 5204},
        env={"USER_GRPC_PORT": "5104", "USER_HTTP_PORT": "5204"},
    ),
    "task-scheduler-service": ServiceSpec(
        id="task-scheduler-service",
        dir_name="task-scheduler-service",
        cmd=("-m", "app.main"),
        ports={"grpc": 5100},
        env={"SCHEDULER_PORT": "5100"},
    ),
    "model-proxy-service": ServiceSpec(
        id="model-proxy-service",
        dir_name="model-proxy-service",
        cmd=("-m", "app.bootstrap"),
        ports={"grpc": 5302},
        env={"MODEL_PROXY_GRPC_PORT": "5302"},
    ),
    "agent-orchestrator-service": ServiceSpec(
        id="agent-orchestrator-service",
        dir_name="agent-orchestrator-service",
        cmd=("-m", "app.main"),
        ports={"grpc": 5300},
        env={"ORCHESTRATOR_GRPC_PORT": "5300"},
    ),
    "gateway-backend-service": ServiceSpec(
        id="gateway-backend-service",
        dir_name="gateway-backend-service",
        cmd=("serve.py",),
        ports={"http": 5210},
        health="http",
    ),
    "dashboard-service": ServiceSpec(
        id="dashboard-service",
        dir_name="dashboard-service",
        cmd=("-m", "app.main"),
        ports={"http": 5700},
        health="http",
    ),
    "tool-runtime-service": ServiceSpec(
        id="tool-runtime-service",
        dir_name="tool-runtime-service",
        cmd=("-m", "app.main"),
        ports={"grpc": 5303},
    ),
    "timer-task-service": ServiceSpec(
        id="timer-task-service",
        dir_name="timer-task-service",
        cmd=("-m", "app.main"),
        ports={"grpc": 5103},
    ),
    "qq-llbot-service": ServiceSpec(
        id="qq-llbot-service",
        dir_name="qq-llbot-service",
        cmd=("-m", "app.main"),
        ports={"grpc": 5106},
    ),
    "qq-official-service": ServiceSpec(
        id="qq-official-service",
        dir_name="qq-official-service",
        cmd=("-m", "app.main"),
        ports={"grpc": 5107},
    ),
    "qq-onebot-service": ServiceSpec(
        id="qq-onebot-service",
        dir_name="qq-onebot-service",
        cmd=("-m", "app.main"),
        ports={},            # 正向模式没有本地端口；反向模式才听 ONEBOT_WS_LISTEN
        env={"ONEBOT_MODE": "forward"},
    ),
    "openviking-context-service": ServiceSpec(
        id="openviking-context-service",
        dir_name="openviking-context-service",
        cmd=("-m", "app.main"),
        ports={"grpc": 5301},
    ),
    "embedding-service": ServiceSpec(
        id="embedding-service",
        dir_name="embedding-service",
        cmd=("-m", "app.main"),
        ports={"grpc": 5305},   # 容器里是 5303，与 tool-runtime 冲突，单体换成 5305
    ),
}

# features.json 里的功能 id → 需要启动的服务（普通功能直接列；带 mode 的由 plan 处理）
FEATURE_SERVICES: dict[str, list[str]] = {
    "core": [
        "user-service",
        "gateway-backend-service",
        "task-scheduler-service",
        "agent-orchestrator-service",
        "model-proxy-service",
        "dashboard-service",
    ],
    "tools.basic": ["tool-runtime-service"],
    "timer": ["timer-task-service"],
    "qq.satori": ["qq-llbot-service"],
    "qq.onebot": ["qq-onebot-service"],
    "memory.local": [],
    "memory.openviking": ["openviking-context-service"],
    "memory.openviking.install": ["openviking-context-service", "embedding-service"],
    "skills": [],
    "browser": [],
    "search": [],
    "external-exec": [],
    "hostfiles": [],
}

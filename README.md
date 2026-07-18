# My_Agent_MSA

> 基于 Kubernetes + gRPC + Python 的 AI Agent 微服务架构，支持多智能体编排、定时任务、实时事件流、多渠道接入。

## 架构概览

```
Browser / QQ
  ↓ HTTP / Satori
frontend-service:80 (nginx)     qq-llbot-service (Satori → gRPC)
  ↓ /api/*                        ↓ gRPC
gateway-backend-service:5210 (FastAPI + SSE)
  ↓ gRPC                         ↓
task-scheduler-service:5100 (槽位调度 + 事件总线)
  ↓ gRPC
agent-orchestrator-service:5300 (Agent 运行时编排)
  ├── openviking-context-service:5301 (RAG 上下文检索)
  ├── model-proxy-service:5302 (统一模型调用 → host.docker.internal:11434)
  ├── tool-runtime-service:5303 (工具执行 + workspace)
  └── timer-task-service:5103 (定时任务)
        └── task-scheduler-service:5100 (到期回调)
user-service:5104 (用户信息)
```

## 服务端口表

| 服务 | 端口 | 协议 | 说明 |
|------|------|------|------|
| frontend-service | 80 | HTTP | Nginx 静态页面 + API 代理 |
| gateway-backend-service | 5210 | HTTP | Web 后端网关，SSE 事件推送 |
| qq-llbot-service | 5601 | Satori/gRPC | QQ 渠道接入，LLBot Satori 协议 |
| qq-satori-adapter | 5600 | HTTP/WS | LLBot Satori 适配器（外部部署） |
| task-scheduler-service | 5100 | gRPC | 任务调度、槽位管理、事件总线 |
| timer-task-service | 5103 | gRPC | 定时任务管理，到期回调 scheduler |
| agent-orchestrator-service | 5300 | gRPC | Agent 运行时编排核心 |
| openviking-context-service | 5301 | gRPC | OpenViking RAG 上下文检索 |
| model-proxy-service | 5302 | gRPC | 统一模型调用适配 |
| tool-runtime-service | 5303 | gRPC | 工具执行、skill、workspace |
| user-service | 5104 | gRPC | 用户信息与多渠道绑定 |
| dashboard-service | 5601 | HTTP | 管理控制面板（FastAPI + Web UI） |

## 核心功能

### 1. 多智能体编排

Agent 运行时支持 `main`、`tool`、`reader` 三种智能体角色，通过 `agent_list.json` 配置模型参数与系统提示词。智能体之间可通过 `对话:target_id|content` 语法互相调用，也支持 `切换:agent_id` 改变默认智能体。

### 2. 语法解析

模型输出通过 `syntax_parser.py` 解析，支持以下协议：

| 协议 | 格式 | 说明 |
|------|------|------|
| 对话 | `对话:target_id\|content` | 切换智能体或回复用户 |
| 工具调用 | `工具调用:tool_name\|arg1\|arg2` | 执行工具 |
| Shell 工具 | `工具调用:shell\|raw command` | 执行 shell 命令（优先级最高） |
| 切换智能体 | `切换:agent_id` 或 `切换到xxx智能体` | 更新默认智能体 |
| 定时任务 | `定时任务:类型\|[agent_id]内容\|时间` | 创建定时任务 |
| 询问 | `询问:xxx` | 向用户提问 |

### 3. 定时任务

支持两种定时任务类型：

- **submit_task**：到期后通过 orchestrator 执行完整 Agent 链路
- **send_message**：到期后直接推送消息给用户（跳过 orchestrator，低延迟）

定时任务语法示例：
```
定时任务:submit_task|[main]提交commit|2026-07-12 10:00:00
定时任务:send_message|每日提醒内容|2026-07-12 08:00:00
```

`[agent_id]` 格式支持指定执行智能体，不填则使用当前默认智能体。

### 4. 实时事件流

- gateway / qq-llbot 通过 gRPC 双向流订阅 scheduler 事件总线
- 前端通过 SSE (`/api/events`) 接收实时推送
- QQ 通过 Satori 协议推送消息
- 事件类型：`task_queued` → `task_started` → `assistant_message` / `assistant_intermediate` → `task_completed`

### 5. 多渠道接入

| 渠道 | channel 值 | 接入方式 |
|------|-----------|----------|
| Web | `web` | gateway-backend-service (FastAPI + SSE) |
| QQ | `qq` | qq-llbot-service (Satori + gRPC) |

`scheduler` 事件总线按 `channel` 字段路由，各渠道网关独立订阅各自频道，互不干扰。

## 目录结构

```
My_Agent_MSA/
├── services/                   # 所有微服务源码
│   ├── agent-orchestrator-service/
│   ├── task-scheduler-service/
│   ├── timer-task-service/
│   ├── gateway-backend-service/
│   ├── qq-llbot-service/
│   ├── model-proxy-service/
│   ├── openviking-context-service/
│   ├── tool-runtime-service/
│   ├── user-service/
│   ├── frontend-service/
│   └── dashboard-service/
├── admin-panel/                # Windows 管理员控制面板（C# WPF）
│   ├── MyAgentAdminPanel.csproj
│   ├── Pages/                  # 端口转发 / 部署 / LLBot / Dashboard
│   ├── Services/               # kubectl / docker / llbot 服务封装
│   ├── Models/
│   ├── build.bat               # 编译脚本
│   └── README.md
├── config/                     # 持久化配置（同步到 NFS）
│   ├── agent_list.json
│   ├── model_list.json
│   └── system_prompt/
├── deploy/                     # 部署相关（脚本 + YAML）
│   ├── setup-nfs.sh            # NFS 初始化
│   ├── sync-config.sh          # 同步 config → NFS
│   ├── apply-pv.sh             # 创建 PV/PVC
│   ├── services/               # K8s 服务 YAML
│   └── tool-runtime-apply.sh   # tool-runtime 外部 VM 部署
├── deploy-all.ps1              # 一键部署（PowerShell）
├── deploy-all.bat              # 一键部署（双击运行）
├── port-forward.bat            # 端口转发（kubectl port-forward）
└── 常见问题处理.md
```

## Windows 管理员面板（admin-panel）

基于 .NET 8 WPF + WebView2 的桌面管理工具，用于日常运维管理。

### 功能

| 页面 | 功能 |
|------|------|
| 🔌 端口转发 | 一键启停 dashboard(5601)、gateway(5210)、istio(8080) |
| 🚀 一键部署 | 从 `deploy-all.ps1` 解析服务列表，多选构建镜像并部署 |
| 🤖 LLBot | 启动本地 `llbot.exe`，实时日志 + 二维码同步推送 |
| 📊 Dashboard | WebView2 内嵌 `http://localhost:5601`，直接访问管理后台 |

### 编译与运行

```bat
cd admin-panel
build.bat
```

产物：`admin-panel/publish/MyAgentAdminPanel.exe`

### 环境要求

- Windows 10 / 11
- .NET 8 Runtime（运行）或 .NET 8 SDK（编译）
- Docker Desktop（已启用 Kubernetes）
- kubectl

### 版本管理

服务版本号集中在项目根 `deploy-all.ps1` 的 `$IMAGES` 中维护。admin-panel 启动时自动解析，无需重新编译。

## 快速开始

### 前置条件

- Kubernetes 集群（推荐 Docker Desktop + WSL2）
- NFS 服务端（WSL 内运行 `deploy/setup-nfs.sh` 初始化）
- Docker（用于构建镜像）
- LLBot / Satori 适配器（QQ 渠道接入需要）

### 一键部署（推荐）

在 Windows 上双击 `deploy-all.bat`，按提示选择要部署的服务，自动完成 `docker build` + `kubectl apply`。

### 手动部署

#### 1. 初始化 NFS（WSL 内执行）

```bash
sudo bash deploy/setup-nfs.sh
```

#### 2. 同步配置到 NFS（WSL 内执行）

```bash
bash deploy/sync-config.sh
```

#### 3. 创建 PV/PVC（WSL 内执行）

```bash
NFS_SERVER=172.29.219.49 NFS_ROOT=/srv/nfs/my-agent bash deploy/apply-pv.sh
```

#### 4. 部署服务

```bash
kubectl apply -f deploy/services/
```

### 本地访问

```bash
# Web 渠道
kubectl -n istio-system port-forward svc/istio-ingressgateway 8080:80
http://localhost:8080/login.html

# Dashboard 管理面板
kubectl -n agent port-forward svc/dashboard-service 5601:5601
http://localhost:5601

# QQ 渠道：确保 LLBot Satori 适配器已部署并配置 SATORI_HOST/SATORI_TOKEN
```

## QQ 渠道配置

`qq-llbot-service` 通过 Satori 协议对接 LLBot，需要预先部署 Satori 适配器。

### 端口映射（WSL2 + Docker Desktop）

LLBot 运行在 Windows 宿主机，使用 `SATORI_HOST=host.docker.internal` 直连。

**Windows 上 LLBot 需要监听 `0.0.0.0:5600`**（不能是 `127.0.0.1`）

### 配置文件

配置文件位于 NFS：`/srv/nfs/my-agent/config/qq-llbot/qq_llbot_config.json`

通过现有 `my-agent-config-pvc`（subPath=qq-llbot）挂载到 Pod 的 `/service/config/`。

修改配置后重启 Pod 生效（`kubectl rollout restart deployment/qq-llbot-service -n agent`）。

### 环境变量

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `SATORI_HOST` | `host.docker.internal` | Satori 适配器地址（Docker Desktop 直连宿主机） |
| `SATORI_PORT` | `5600` | Satori WebSocket 端口 |
| `SATORI_TOKEN` | `""` | Satori 认证 token |
| `SCHEDULER_TARGET` | `task-scheduler-service.agent.svc.cluster.local:5100` | 调度器地址 |

### 数据流

`QQ 消息 → qq-llbot-service (Satori) → scheduler.CreateTask(channel="qq") → orchestrator → scheduler.SubscribeEvents(channels=["qq"]) → qq-llbot-service → QQ 回复`

## 当前镜像版本

| 服务 | 镜像 | 版本 |
|------|------|------|
| dashboard-service | `agent/dashboard-service` | v5 |
| agent-orchestrator-service | `agent/agent-orchestrator-service` | v11 |
| task-scheduler-service | `agent/task-scheduler-service` | v5 |
| timer-task-service | `agent/timer-task-service` | v2 |
| gateway-backend-service | `agent/gateway-backend-service` | v4 |
| qq-llbot-service | `agent/qq-llbot-service` | v1 |
| frontend-service | `agent/frontend-service` | v1 |
| model-proxy-service | `agent/model-proxy-service` | v3 |
| openviking-context-service | `agent/openviking-context-service` | v17 |
| tool-runtime-service | `agent/tool-runtime-service` | v1 |
| user-service | `agent/user-service` | v1 |

## NFS 挂载说明

K8s PV 中 NFS 配置示例：

```yaml
nfs:
  server: 172.29.219.49
  path: /srv/nfs/my-agent/workspace
```

NFS 目录结构：
```
/srv/nfs/my-agent/
├── config/          # 智能体配置 & 系统提示词
├── openviking/      # OpenViking 向量数据库
├── assets/          # 静态资源
├── qq-llbot/       # QQ LLBot 配置（token 等）
├── workspace/       # 工具执行 workspace
├── timer-tasks/     # 定时任务持久化
└── user-data/       # 用户数据
```

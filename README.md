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
  ├── model-proxy-service:5302 (统一模型调用)
  │     └── model-serving:8000 (Ollama / vLLM)
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
| qq-llbot-service | — | Satori/gRPC | QQ 渠道接入，LLBot Satori 协议 |
| qq-satori-adapter | 5600 | HTTP/WS | LLBot Satori 适配器（外部部署） |
| task-scheduler-service | 5100 | gRPC | 任务调度、槽位管理、事件总线 |
| timer-task-service | 5103 | gRPC | 定时任务管理，到期回调 scheduler |
| agent-orchestrator-service | 5300 | gRPC | Agent 运行时编排核心 |
| openviking-context-service | 5301 | gRPC | OpenViking RAG 上下文检索 |
| model-proxy-service | 5302 | gRPC | 统一模型调用适配 |
| tool-runtime-service | 5303 | gRPC | 工具执行、skill、workspace |
| user-service | 5104 | gRPC | 用户信息与多渠道绑定 |
| model-serving | 8000 | HTTP | Ollama / vLLM 兼容 API |

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
├── frontend-service/           # Nginx 前端静态服务
├── gateway-backend-service/    # FastAPI Web 网关 + SSE
├── qq-llbot-service/           # QQ LLBot 渠道网关
│   ├── app/
│   │   ├── main.py             # 入口：并行启动 Satori + gRPC 事件订阅
│   │   ├── qq_bridge.py        # Satori QQ 消息收发
│   │   ├── scheduler_client.py # 异步 gRPC 客户端
│   │   └── config.py           # 环境变量配置
│   ├── proto/                  # task_scheduler.proto
│   ├── scripts/gen_proto.sh    # proto 生成脚本
│   ├── Dockerfile
│   └── requirements.txt
├── task-scheduler-service/     # gRPC 任务调度服务
├── agent-orchestrator-service/ # gRPC Agent 编排服务
├── openviking-context-service/ # gRPC 上下文检索服务
├── model-proxy-service/        # gRPC 模型代理服务
├── tool-runtime-service/       # gRPC 工具运行服务
├── timer-task-service/         # gRPC 定时任务服务
├── user-service/               # gRPC 用户服务
├── config/                     # NFS 持久化配置
│   ├── agent_list.json         # 智能体配置
│   ├── model_list.json         # 模型列表
│   └── system_prompt/          # 系统提示词
├── k8s_yaml/                   # Kubernetes 部署清单
├── nfs-pv-template-apply/      # NFS PV/PVC 模板
└── setup_my_agent_nfs.sh       # NFS 环境初始化脚本
```

## 快速开始

### 前置条件

- Kubernetes 集群（推荐 Docker Desktop + WSL2）
- NFS 服务端（运行 `setup_my_agent_nfs.sh` 初始化）
- Docker（用于构建镜像）
- LLBot / Satori 适配器（QQ 渠道接入需要）

### 1. 初始化 NFS

```bash
sudo ./setup_my_agent_nfs.sh
```

### 2. 构建镜像

```bash
docker build -t agent/agent-orchestrator-service:v11 agent-orchestrator-service/
docker build -t agent/task-scheduler-service:v5 task-scheduler-service/
docker build -t agent/timer-task-service:v2 timer-task-service/
docker build -t agent/gateway-backend-service:v4 gateway-backend-service/
docker build -t agent/qq-llbot-service:v1 qq-llbot-service/
docker build -t agent/model-proxy-service:v1 model-proxy-service/
docker build -t agent/openviking-context-service:v1 openviking-context-service/
docker build -t agent/tool-runtime-service:v1 tool-runtime-service/
docker build -t agent/user-service:v1 user-service/
docker build -t agent/frontend-service:v1 frontend-service/
```

### 3. 部署到 K8s

```bash
kubectl apply -f k8s_yaml/
```

### 4. 本地访问

```bash
# Web 渠道
kubectl -n istio-system port-forward svc/istio-ingressgateway 8080:80
http://localhost:8080/login.html

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
| agent-orchestrator-service | `agent/agent-orchestrator-service` | v11 |
| task-scheduler-service | `agent/task-scheduler-service` | v5 |
| timer-task-service | `agent/timer-task-service` | v2 |
| gateway-backend-service | `agent/gateway-backend-service` | v4 |
| qq-llbot-service | `agent/qq-llbot-service` | v1 |
| frontend-service | `agent/frontend-service` | v1 |
| model-proxy-service | `agent/model-proxy-service` | v1 |
| openviking-context-service | `agent/openviking-context-service` | v1 |
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

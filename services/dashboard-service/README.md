# dashboard-service

My_Agent MSA 的管理控制面板：FastAPI 后端 + 静态 Web UI，负责配置管理、集群状态查看、日志、System Prompt 管理、Session 管理、长期记忆查询等日常运维功能。

## 端口与命名

```text
namespace: agent
image: agent/dashboard-service:v19
service: dashboard-service
port: 5700 (HTTP)
访问: kubectl -n agent port-forward svc/dashboard-service 5700:5700 → http://localhost:5700
```

## 功能

| 模块 | API | 说明 |
|------|-----|------|
| 认证 | `POST /api/auth/login` / `POST /api/auth/change-password` | 面板登录与改密，密码存于 config 卷 `dashboard_password.json`，默认 `123456` |
| 配置管理 | `GET/PUT /api/config/{path}`、`GET /api/config` | 读写 `CONFIG_ROOT` 下的配置文件（排除 system_prompt 与 process 目录） |
| Pod/Service 状态 | `GET /api/pods`、`GET /api/services` | 调用 kubectl 查询集群状态 |
| 重启 | `POST /api/restart?service=xxx` | `kubectl rollout restart deployment` |
| 日志 | `GET /api/logs?pod=xxx`、`GET /api/logs/stream?pod=xxx` | 单次拉取 / SSE 流式日志 |
| 部署脚本 | `GET /api/deploy/script?services=a,b` | 生成一键部署 shell 脚本 |
| System Prompt | `GET/POST/PUT/DELETE /api/system_prompt/**` | 管理每个智能体的 System Prompt 文件 |
| 二维码 | `POST/GET /api/qrcode`、`GET /api/qrcode/info` | 接收 admin-panel 推送的 LLBot 登录二维码并展示 |
| Session 管理 | `GET /api/session/**` | 按 openviking 用户列出/查看/删除 session 及 messages.jsonl |
| 长期记忆 | `GET /api/memory/search?user=&query=` | 调 OpenViking `/api/v1/search/find` 做用户级语义搜索 |
| 杂项设置 | `GET/PUT /api/misc/settings` | 读写 orchestrator 的 system_settings.json（图片接收、shell 限制等开关） |

## 环境变量

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `CONFIG_ROOT` | `/app/config` | 配置根目录（挂载 my-agent-config-pvc） |
| `NAMESPACE` | `agent` | kubectl 查询的命名空间 |
| `SESSION_ROOT` | `/app/session-data/workspace/viking/my-agent/user` | openviking 用户 session 根目录（挂载 my-agent-openviking-pvc） |
| `OPENVIKING_SERVER_URL` | `http://openviking.agent.svc.cluster.local:1933` | OpenViking 语义搜索服务地址 |
| `USER_SERVICE_URL` | `http://user-service.agent.svc.cluster.local:5204` | user-service HTTP 地址（取 per-user OpenViking key） |
| `OPENVIKING_API_KEY` | `/app/config/openviking/api_key` | OpenViking key 文件路径（用户无独立 key 时回退） |

## 依赖

- 镜像内安装 `kubectl`，Pod 通过 `dashboard-sa` ServiceAccount 访问集群。
- RBAC（见 `deploy/services/dashboard-service.yaml`）：`pods`、`pods/log`、`services`、`endpoints`、`namespaces` 只读 + `deployments` 可 patch（重启用）。
- 依赖两个 PVC：`my-agent-config-pvc`（/app/config）、`my-agent-openviking-pvc`（/app/session-data）。

## 构建镜像

```bash
cd dashboard-service
docker build -t agent/dashboard-service:v19 .
```

## 部署

```bash
kubectl apply -f deploy/services/dashboard-service.yaml
```

## 检查

```bash
kubectl get pods -n agent -l app=dashboard-service
kubectl logs -n agent deployment/dashboard-service
kubectl -n agent port-forward svc/dashboard-service 5700:5700
curl http://localhost:5700/api/pods
```

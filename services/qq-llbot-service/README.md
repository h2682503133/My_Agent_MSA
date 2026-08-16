# qq-llbot-service

My_Agent MSA 的 QQ 渠道接入服务：通过 Satori 协议对接 LLBot 适配器收发 QQ 消息，将用户消息提交给 `task-scheduler-service`，并把调度器事件流推回 QQ。

## 架构位置

```text
QQ 用户
  ↓ Satori (WebSocket)
qq-satori-adapter:5600（外部部署的 LLBot 适配器）
  ↓ Satori
qq-llbot-service（本服务，Satori 客户端，不监听端口）
  ├─ 收到 QQ 消息 → scheduler.CreateTask(channel="qq")
  └─ SubscribeEvents(channels=["qq"]) → assistant_message 等事件 → 推回 QQ
```

## 环境变量 / 配置

配置文件默认位于 `/service/config/qq_llbot_config.json`（通过 `my-agent-config-pvc` 的 `subPath: qq-llbot` 挂载，对应 NFS `/srv/nfs/my-agent/config/qq-llbot/qq_llbot_config.json`），JSON 值可被环境变量覆盖。

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `SCHEDULER_TARGET` | `task-scheduler-service.agent.svc.cluster.local:5100` | 调度器 gRPC 地址 |
| `SATORI_HOST` | `qq-satori-adapter` | Satori 适配器地址（Docker Desktop 环境常用 `host.docker.internal`） |
| `SATORI_PORT` | `5600` | Satori WebSocket 端口 |
| `SATORI_TOKEN` | `""` | Satori 认证 token |
| `SUBSCRIBER_ID` | `qq-llbot-1` | 事件订阅者 ID（断线重连后恢复事件流） |
| `GROUP_AT_REQUIRED` | `true` | 群聊消息是否必须 @ 机器人（私聊不受影响） |

## 行为细节

- **会话 key**：私聊 `qq_{user_id}`，群聊 `qq_g_{channel_id}`，不同群上下文互不干扰，回复发回原群。
- **群聊触发**：仅在被明确 @ 机器人时下发（`@全体`/`@here` 不触发）。
- **图片**：图片 URL 抓取后转为 base64 data URL 随任务提交（gRPC 单条消息上限放宽到 128 MiB）；回复中的图片直接以 URL 形式推送。
- **事件推送**：`assistant_message` / `assistant_intermediate`（`visible_to_user=true`）推送正文；`task_waiting_user` 推送询问；失败/超时/取消等终态事件推送「任务失败」提示，避免静默无响应。
- **断线重连**：事件订阅断开后每 2 秒自动重连。

## 本地 / 容器内启动

```bash
pip install -r requirements.txt
python -m app.main
```

## 构建镜像

```bash
cd qq-llbot-service
docker build -t agent/qq-llbot-service:v5 .
```

## 部署

```bash
kubectl apply -f deploy/services/qq-llbot-service.yaml
kubectl rollout restart deployment/qq-llbot-service -n agent   # 修改配置后重启生效
```

## 检查

```bash
kubectl get pods -n agent -l app=qq-llbot-service
kubectl logs -n agent deployment/qq-llbot-service
```

## 前置条件

- LLBot（Windows 宿主机）需监听 `0.0.0.0:5600`（不能是 `127.0.0.1`），且 `SATORI_HOST=host.docker.internal` 可直连。
- scheduler（`task-scheduler-service:5100`）可用，事件总线按 `channel=qq` 路由。

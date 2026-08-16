# timer-task-service

My_Agent MSA 的独立定时任务服务：接收定时任务创建请求，任务到期后通过 gRPC 回调 `task-scheduler-service` 触发执行。已从 `task-scheduler-service` 中拆出，职责单一、可独立部署。

## 端口与命名

```text
namespace: agent
image: agent/timer-task-service:v2
service: timer-task-service
port: 5103 (gRPC)
```

## gRPC 接口（proto/timer_task.proto）

| RPC | 说明 |
|-----|------|
| `CreateTimerTask` | 创建定时任务（trigger_timestamp 为 Unix 秒级时间戳） |
| `ListUserTasks` | 列出某用户全部定时任务（按触发时间排序） |
| `DeleteUserTask` | 删除指定定时任务 |

任务以 JSON 文件落盘于 `TIMER_TASK_DIR`（`task_{毫秒时间戳}_{user_id}.json`）。

## 执行流程

```text
CreateTimerTask(user_id, trigger_timestamp, content, task_type, ...)
  → 落盘 JSON
  → 后台扫描线程（timer_scan_loop）发现到期任务
  → 回调 scheduler.CreateTask(channel=channel_id, ...)
  → 删除任务文件
```

- **task_type = `submit_task`**：到期后走完整 Agent 链路（orchestrator）。
- **task_type = `send_message`**：直接推送消息（metadata 带 `source=timer_task`、`timer_type=send_message`）。
- **智能扫描**：新增任务后 30 秒内快速扫描（间隔 `TIMER_SCAN_INTERVAL_FAST`）；无临近任务（180 秒内）时自动降为慢扫描（`TIMER_SCAN_INTERVAL_SLOW`）。

## 环境变量

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `TIMER_GRPC_HOST` | `0.0.0.0` | gRPC 监听地址 |
| `TIMER_GRPC_PORT` | `5103` | gRPC 监听端口 |
| `TIMER_TASK_DIR` | `./roaming/tasks`（K8s 中 `/data/tasks`） | 定时任务 JSON 存储目录（挂载 my-agent-timer-tasks-pvc） |
| `TIMER_SCAN_INTERVAL_FAST` | `5` | 快速扫描间隔（秒） |
| `TIMER_SCAN_INTERVAL_SLOW` | `60` | 慢速扫描间隔（秒） |
| `SCHEDULER_TARGET` | `task-scheduler-service.agent.svc.cluster.local:5100` | 回调的调度器 gRPC 地址 |
| `SCHEDULER_GRPC_DEADLINE` | `10` | 回调超时（秒） |

## 本地启动

```bash
pip install -r requirements.txt
bash scripts/gen_proto.sh          # 生成 app/generated 下的 gRPC 代码
python -m app.main
```

## 构建镜像

```bash
cd timer-task-service
docker build -t agent/timer-task-service:v2 .
```

## 部署

```bash
kubectl apply -f deploy/services/timer-task-service.yaml
```

## 检查

```bash
kubectl get pods -n agent -l app=timer-task-service
kubectl get svc timer-task-service -n agent
kubectl logs -n agent deployment/timer-task-service
```

## 调用方

- `agent-orchestrator-service` 解析「定时任务:」语法后，通过 gRPC `CreateTimerTask` 创建任务。
- 任务到期后本服务回调 scheduler，与前端/QQ 渠道共用同一条任务链路。

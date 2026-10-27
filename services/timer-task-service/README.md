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
| `CreateTimerTask` | 创建定时任务（`time_str` 开始时间 + `repeat_str` 重复计划，或 `trigger_timestamp` Unix 秒级时间戳） |
| `ListUserTasks` | 列出某用户全部定时任务（按触发时间排序，带 `schedule_str` 重复描述） |
| `DeleteUserTask` | 删除指定定时任务 |

协议格式：`定时任务:任务类别|智能体id|任务内容|开始时间|重复计划`

**第 4 参 = 开始时间**（`time_str`，空=立即，也可写 `现在`）：

- 绝对时间：`2026-01-31 10:00:00` / `2026/01/31 10:00`
- 当天时间：`10:00` / `10点30分` / `下午3点`（已过顺延明天）
- 相对时间：`5分钟后` / `2小时后` / `明天10:00` / `后天9点`
- 区间随机：`10:00-11:00`（当天随机时刻）/ `5-10分钟后`（随机延迟）

**第 5 参 = 重复计划**（`repeat_str`，可选；空或 `0` = 不重复）：

- 单一时长：`每10秒` / `每30分钟` / `每2小时` / `每天`
- 混合单位：`每1小时30分钟`
- 区间随机时长：`每5-10分钟` / `每1-2小时`
- 混合单位区间：`每5分钟-2小时` / `每1小时30分钟-2小时`（区间两端可用不同单位）

兼容旧格式：第 4 参直接写重复计划（如 `每30分钟`）且第 5 参为空时，视为立即开始 + 该重复计划。

任务以 JSON 文件落盘于 `TIMER_TASK_DIR`（`task_{毫秒时间戳}_{user_id}.json`）。
重复任务（带 `schedule` 字段）到期执行后按间隔自动重排下一次触发时间，不删除文件。

## 执行流程

```text
CreateTimerTask(user_id, time_str|trigger_timestamp, content, task_type, ...)
  → time_parser 解析（首次触发时间 + 重复计划）
  → 落盘 JSON
  → 后台扫描线程（timer_scan_loop）发现到期任务
  → 回调 scheduler.CreateTask(channel=channel_id, ...)
  → 一次性任务删除文件；重复任务按 schedule 重排下一次触发
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

## 执行时段（第 6 参，可选）

```
定时任务:任务类别|智能体id|任务内容|开始时间|重复计划|执行时段
```

前 5 参与原来完全一致（**随机算法不变**）；第 6 参给一个时间区间（执行时段），例如 `09:00-22:00`：

- 到点触发时，若**当前时刻不在窗口内 → 不执行**（不发消息、不报错、不补发），
  等下一次触发；
- 一次性任务在窗口外触发时直接作废（没有"下一次"）；
- 支持跨天窗口（`22:00-02:00`，起 > 止 即视为跨天）；
- 省略该参数 = 不限制（旧行为）。

例子：

```
定时任务:send_message|main|喝水提醒|9:00|每2小时|09:00-22:00
定时任务:submit_task|main|每日总结|20:00|每天|20:00-23:30
```

存储：任务 JSON 里多两个字段 `window`（`[起始秒, 结束秒]`）与 `window_str`（人类可读）。
判定函数：`time_parser.in_window()` / `get_task_window()`（无窗口恒 True）。
扫到点时的判定在 `timer_task.timer_scan_loop`，调用 `in_task_window(task, now)`。

自检：`tools/timer_window_test.py`（16 项：解析、边界、跨天、任务判定）。

### 时段外策略（`TIMER_MISSED_WINDOW_POLICY`，默认 realign）

随机算法完全不动；只处理"下一次触发落在窗口外"的情况：

| 情况 | 处理 |
|---|---|
| 下一次触发仍在窗口内 | 不动 |
| 落在窗口外 | 锚到**下一个窗口起点**，再叠加一次重复偏移（`interval_range` 的随机值） |
| 偏移 >= 窗口长度（如 `每天` 86400 vs 13h 窗口） | 退回窗口起点，避免平白跳过一整天 |
| 窗口内起点 + 偏移仍在窗口内（如 `每2小时`） | 用它，保留原有节奏 |
| 一次性任务在窗口外触发 | 直接作废（没有"下一次"） |

实测（窗口 `09:00-22:00`，当前 23:10）：

```
每天    → 次日 09:00     （不是后天，今天的机会不被吞掉）
每2小时 → 次日 11:00     （起点 + 2h，节奏保留）
每30分钟 → 次日 09:30
```

改成严格模式（错过就错过）：

```bash
export TIMER_MISSED_WINDOW_POLICY=skip
```

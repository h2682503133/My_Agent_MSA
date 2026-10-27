# task-scheduler-service

从 `h2682503133/My_Agent` 原项目中拆出的任务调度微服务。

本服务对应微服务架构中的 `task-scheduler-service`，职责只保留调度层：

- 接收 `gateway-backend-service` / 各 `channel-gateway-service` 的 `CreateTaskRequest`
- 创建轻量 `ScheduledTask`，不创建原项目里的重型 `Task`
- 维护用户级队列、批处理槽位、忙碌用户锁
- 通过 gRPC streaming 调用 `agent-orchestrator-service.ExecuteTask`
- 将 orchestrator 返回的 `TaskEvent` 发布给已订阅的 gateway / channel-gateway
- 保留原项目无重试、单次执行、同一用户串行、全局并发槽位的调度语义

## 关键边界

### scheduler 只持有轻量任务对象

`app/scheduled_task.py` 中的 `ScheduledTask` 只包含调度和路由需要的数据：

```text
task_id
user_id
session_id
channel
content
client_message_id
delivery_target
metadata
status / waiting / slot_index / retry_count
```

它不包含以下 Agent Runtime 状态：

```text
agent_context
push_context()
pop_context()
temp_dialog_input / temp_dialog_output
main_memory
tool_log
main_log
Task.task_map
User.send()
```

这些状态应该由 `agent-orchestrator-service` 在收到 `ExecuteTaskRequest` 后构造 `TaskRuntime` 时负责。

### orchestrator 负责真正的 Agent TaskRuntime

原项目的 `core/Task/Task.py` 更适合作为 orchestrator 内部的运行时对象迁移依据，因为它包含压栈/弹栈、pending、工具日志和智能体运行状态。

本服务的 `legacy_original/core/Task/Task.py` 仅作为原文备份和迁移参考，不参与 `task-scheduler-service` 运行。

## 目录结构

```text
.
├── app
│   ├── main.py                    # gRPC 服务启动入口
│   ├── server.py                  # TaskScheduler gRPC 服务实现
│   ├── scheduler.py               # 从原 core/Task/scheduler.py 拆出的调度核心
│   ├── scheduled_task.py          # 轻量 ScheduledTask / DeliveryTarget DTO
│   ├── event_bus.py               # SubscribeEvents 所需事件总线
│   ├── orchestrator_client.py     # 调用 agent-orchestrator-service
│   ├── timer_task.py              # 定时任务，提交 ScheduledTask
│   ├── config.py                  # 环境变量配置
│   ├── logger.py                  # 从原 core/logger.py 简化出的 gateway_log
│   └── generated/.gitkeep         # protoc 生成目录
├── proto
│   ├── task_scheduler.proto       # gateway <-> scheduler 协议
│   └── agent_orchestrator.proto   # scheduler <-> orchestrator 协议
├── scripts
│   └── gen_proto.sh               # 生成 Python gRPC 代码
├── legacy_original
│   └── core/Task                  # 原项目 Task 相关文件备份，便于对照
├── requirements.txt
└── Dockerfile
```

## 生成 gRPC 代码

```bash
pip install -r requirements.txt
bash scripts/gen_proto.sh
```

## 本地启动

```bash
export SCHEDULER_HOST=0.0.0.0
export SCHEDULER_PORT=5100
export ORCHESTRATOR_TARGET=agent-orchestrator-service:5300
python -m app.main
```

## Docker 构建

```bash
docker build -t agent/task-scheduler-service:v12 .
docker run --rm -p 5100:5100 \
  -e ORCHESTRATOR_TARGET=agent-orchestrator-service:5300 \
  agent/task-scheduler-service:v12
```

## 关键数据流

```text
gateway-backend-service / qq-gateway-service / other channel gateway
  └─ CreateTask(CreateTaskRequest)
      ↓
task-scheduler-service
  ├─ 创建 ScheduledTask
  ├─ 入 USER_QUEUES[user_id]
  ├─ slot_scheduler 按 BATCH_SIZE 分配槽位
  ├─ ExecuteTask 调 agent-orchestrator-service
  └─ SubscribeEvents stream TaskEvent 给 gateway/channel-gateway
      ↓
agent-orchestrator-service
  └─ 构造 TaskRuntime，负责 push_context / pop_context / tool_log / main_log
```

## 保留原调度语义的位置

`app/scheduler.py` 保留了原 `scheduler.py` 中的核心思想：

- `USER_QUEUES`
- `BATCH_SIZE`
- `BATCH_SLOTS`
- `BUSY_USERS`
- `USER_LOCK` / `BUSY_LOCK` / `SLOTS_LOCK`
- `slot_scheduler()`
- `run_task()`
- `submit_task()`
- `start_scheduler()`

同时做了两个服务化修正：

1. 原来的 `process_user_task(task)` 被替换为 `orchestrator_client.execute_task(scheduled_task)`。
2. 原来的 `MESSAGE_QUEUE` 唤醒队列被替换为 `threading.Condition`，避免只 put 不 get 导致信号队列堆满。

## CreateTask 幂等

如果上游提供 `client_message_id`，scheduler 会用：

```text
(user_id, session_id, client_message_id)
```

作为幂等键，避免 gateway 或 Istio retry 导致同一条用户消息重复入队。

## 定时任务

原 `timer_task.py` 已迁移到 `app/timer_task.py`。默认不启动；需要时设置：

```bash
export ENABLE_TIMER_TASKS=true
export TIMER_TASK_DIR=./roaming/tasks
```

迁移后的区别：原代码通过 HTTP 调 `main /submit_task`，这里改为直接构造 `ScheduledTask` 并调用 `submit_task(scheduled_task)`，仍然不构造 Agent Runtime Task。

## 渠道投递与回退

任务事件按 `event.channel` 找对应订阅者直投（`qq-llbot-1` / `qq-official-1` / `web-gateway-1` …）。
直投失败（无订阅者、队列满、消费端 ack 失败）时会走**跨渠道转投**：

1. 用 user-service 里该用户绑定的渠道列表，按 `priority` 升序挑下一个；
2. 跳过已尝试过的渠道与事件显式屏蔽的渠道（`metadata.suppress_channels`）；
3. 按目标渠道重建身份与 session（`user_id` 改写成该渠道的 `channel_user_id`，
   `session_id` 由 `_channel_session_id()` 重算）；
4. 全部失败则进缓冲（按用户），渠道恢复后按目标渠道回放；超过尝试上限丢弃。

### 定时任务允许的渠道

`SCHEDULED_EXCLUDED_CHANNELS`（env，逗号分隔）声明**定时任务结果不允许投递**的渠道，默认**空**。

历史上默认屏蔽 `qqofficial`（当时认为官方主动消息额度紧张、定时推送等于丢消息）。
实测额度足够，故已放开：

- Bot 维度：企业/个人身份证认证 **10/qps**；未认证 **5/qps 且 30/qpm**
- 单关系维度：**20/qpm**，每个好友每天最多 **1000 条**
- 互动召回（`is_wakeup`）：用户主动对话后 30 天内，每周期可下发 1 条（当天 / 1-3 天 / 3-7 天 / 7-30 天）

因此 `qqofficial` 现在既能**直接接收定时消息**，也能作为 llbot 掉线后的**转投渠道**
（前提：该用户绑定了 `qqofficial` 的 openid，且 `qq-official-1` 在线）。

需要收回时：

```bash
export SCHEDULER_EXCLUDED_CHANNELS=qqofficial      # 可写多个，逗号分隔
```

相关自检：`tools/scheduled_channel_default_smoke_test.py`（新默认 + 转投 + 显式收回）、
`tools/scheduled_channel_suppression_smoke_test.py`（屏蔽机制本身）。

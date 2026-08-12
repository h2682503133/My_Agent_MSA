# 工作进度 — 询问挂起/恢复（pause & resume）

> 本文件为设计思路草稿，基于当前代码阅读（2026-08-12），供后续实现参考。
> 覆盖了之前的图床进度记录，如需历史内容见 git。

## 一、目标

把 `询问:xxx` 从「结束本轮任务」改成真正的「挂起等待用户回复」：

1. 模型输出 `询问:xxx` 时，任务进入 suspended（挂起）状态，不结束。
2. 挂起的任务继续**占着该用户队列的位置**（用户回复时能找回它继续跑），
3. 但**释放执行槽位（BATCH_SLOTS）与 BUSY_USERS**，让其他用户的任务可以执行。
4. 用户回复后，任务**从挂起处恢复**：用户回复作为输入推回上下文栈，agent 继续执行（可继续调工具、可再次询问、最终给最终答复）。

## 二、现状与约束（代码事实）

### 2.1 orchestrator 侧
- 微服务化后每个 `ExecuteTask` 是独立请求：`TaskRuntime`（含 `agent_context` 栈、`main_memory`、`tool_log`、`temp_dialog_*`）**每次请求新建，不跨请求持久化**。
- `AgentRuntime` 实例本身有内存缓存（按 session 缓存 agent 实例），但 `TaskRuntime` 没有。
- 当前 `询问:` 分支（`agent_runtime.py send()`）：
  ```python
  elif result["question"]:
      question = ... or "请补充必要信息后我再继续。"
      # 注释明确写着：TaskRuntime 的 agent_context 不会跨请求持久化，
      # 不能再把本轮任务置为 pause 后等待恢复。
      self._emit_user_message(task, emit, question, final=True, agent_id=self.id)
      return
  ```
- `process_task` 主循环：弹栈到 user 对象就 `_emit_user_message(final=True)` 结束；挂起/恢复需要绕开这个「弹栈到 user 即结束」的路径。
- `ExecuteTask` 已是 server-streaming（上一轮改造），事件边产生边 yield，适合推送 `task_waiting_user`。

### 2.2 scheduler 侧（`task-scheduler-service`）
- 调度结构：`USER_QUEUES[user_id]`（每用户一个队列）+ 全局 `BATCH_SLOTS`（BATCH_SIZE 个槽）+ `BUSY_USERS`（每用户同时只能跑一个任务，防止同用户并发）。
- `run_task()` 循环 `client.execute_task(task)` 的事件流，逐条 `event_bus.publish`；`finally` 里释放槽位、`BUSY_USERS.discard(user_id)`、`processed -= 1`。
- **关键**：目前无论任务正常/失败，最终都会释放槽位。挂起时需要在收到 waiting 事件时提前释放，且**不发布 task_completed**。
- `ScheduledTask` 只是轻量调度 DTO，明确不含 agent 运行时状态（注释写明边界）。

### 2.3 gateway / 前端
- gateway 已把 scheduler 事件流即时转发到 SSE；前端 `chat.js` 已有 `case "task_waiting_user"`（展示「需要你补充信息」），可直接复用展示提问。
- 用户回复 = 普通 `CreateTask` 新消息（`content` 为用户输入）。

## 三、设计草案（当前想法）

### 3.1 状态存放：orchestrator 内存注册表
- `agent_context` 里存的是 **AgentRuntime 对象引用**，无法序列化跨服务传输，因此挂起状态必须留在 orchestrator 内存里。
- 新增 orchestrator 侧注册表：`PENDING_TASKS: dict[task_id, TaskRuntime]`（进程内存）。
- scheduler 只保留轻量挂起记录：`SUSPENDED_TASKS[user_id] = ScheduledTask`（含原 task_id / delivery_target / 原 agent_id 等），用于路由恢复，不涉及 agent 状态。

### 3.2 事件协议
- orchestrator 在 `询问:` 分支改为：
  1. 注册 `PENDING_TASKS[task_id] = task`（保留 agent_context / main_memory / tool_log / temp_dialog 等）。
  2. 发射 `task_waiting_user` 事件（text=提问，metadata 带 `task_id`、`suspend=true`）。
  3. **return，不弹栈、不 `_emit_user_message(final=True)`、不 append_turn**。
- scheduler 收到 `task_waiting_user`：
  1. 释放 `BATCH_SLOTS[task.slot_index] = None`、`BUSY_USERS.discard(user_id)`、`processed -= 1`。
  2. `SUSPENDED_TASKS[user_id] = task`。
  3. 把 waiting 事件继续 publish 给 gateway（用户看到提问）。
  4. **不补发 task_completed**（现有 run_task 末尾会检查 saw_terminal_event 补发，挂起事件应排除）。

### 3.3 恢复路由
- 用户回复 → gateway `CreateTask` → scheduler `submit_task`：
  - 先查 `SUSPENDED_TASKS[user_id]`：
    - 有 → 不排队，直接走恢复：构造恢复请求给 orchestrator，原 task_id 继续。
    - 无 → 现有逻辑（排队 + 槽位调度）。
- orchestrator 恢复入口（二选一，倾向 A）：
  - **A. 新增 RPC** `rpc ResumeTask(ResumeTaskRequest) returns (stream TaskEvent)`，`ResumeTaskRequest{task_id, user_id, session_id, content, metadata}`。
  - B. `ExecuteTaskRequest` 增加 `resume_task_id` 字段，语义分叉（不干净）。
- 恢复逻辑：
  1. 从 `PENDING_TASKS` 取出 TaskRuntime；找不到则回退为普通新任务（容错）。
  2. 把用户回复 push 进上下文：`task.set_temp_dialog_input(回复内容)`（或按原栈结构 push_context），`main_memory` 追加 `→user: 回复`。
  3. 继续 `process_task` 剩余循环（从挂起处的 agent 继续 send）。

### 3.4 多轮询问 / 多次挂起
- 同一任务可多次询问：恢复后若再次 `询问:`，再次注册/覆盖 `PENDING_TASKS[task_id]` 即可，scheduler 的 `SUSPENDED_TASKS[user_id]` 重新指向同一 task。
- 每次恢复都沿用原 task_id；事件流对前端表现是「提问 → 用户回复 → 继续」。

### 3.5 释放槽位后的并发语义（待确认）
- 挂起期间同用户又来新消息（不是回答，而是新请求）：策略待定——
  - 直接排队等挂起任务恢复完？还是覆盖挂起？
  - 最简单：挂起任务占着用户队列位，新任务继续排队（复用现有 USER_QUEUES），恢复任务完成后再跑。
- 其他用户不受影响：槽位已释放，正常调度。

## 四、待确认问题（需要用户拍板）

1. **挂起期间用户发新消息**：视为回答（恢复）还是排队？如何区分？—— 目前设计视为回答直接恢复。
2. **挂起任务 TTL**：长时间不回复是否自动丢弃/超时？（防内存泄漏，建议加 TTL，如 24h。）
3. **多次询问时**：前端如何区分「新提问」与「恢复后的继续」？（事件类型/元数据。）
4. **恢复后再次询问**是否继续占同一队列位（建议是）。
5. **BUSY_USERS 语义**：挂起时移除 BUSY_USERS 后，同用户的新任务若排队，恢复任务与新任务是否可能并发？（槽位调度应保证不并发：BUSY_USERS 在恢复任务运行时重新加入。）

## 五、初步改动文件清单

| 服务 | 文件 | 改动 |
|---|---|---|
| orchestrator | `proto/agent_orchestrator.proto` | 新增 `ResumeTask`（或扩展 ExecuteTask） |
| orchestrator | `app/server.py` | `PENDING_TASKS` 注册表 + `ResumeTask` handler |
| orchestrator | `app/agent_runtime.py` | `询问:` 分支改为挂起（注册 + 发 `task_waiting_user`）；恢复续跑逻辑 |
| orchestrator | `app/task_runtime.py` | 可能需要「从挂起状态续跑」辅助方法（push 回复、恢复循环） |
| scheduler | `proto/task_scheduler.proto` | 若走 ResumeTask 转发需新增 RPC/请求字段 |
| scheduler | `app/scheduler.py` | `SUSPENDED_TASKS` 映射；waiting 事件时释放槽位；submit_task 恢复路由；TTL 清理 |
| scheduler | `app/orchestrator_client.py` | `resume_task()` 调用 |
| scheduler | `app/scheduled_task.py` | 可能加 `suspended` 状态标记 |
| gateway | `app.py` / `scheduler_client.py` | 基本不用改（事件流转已通）；确认 CreateTask 透传即可 |
| 前端 | `services/frontend-service/chat.js` | `task_waiting_user` 已有；可优化「等待回复」UI |

## 六、风险与注意点

- 内存注册表 `PENDING_TASKS`：单实例内存，orchestrator 重启会丢；接受（与现有 AgentRuntime 缓存同级别），后续可考虑持久化。
- `process_task` 的「弹栈到 user 即结束」路径：挂起时不能弹栈，恢复时也不能从头 `process_task`（会重跑 first_call），需要把主循环改成可续跑（入口参数/断点）。
- scheduler 的 `saw_terminal_event` 补发逻辑：`task_waiting_user` 不能算 terminal，需要排除，否则会补发 task_completed 导致前端误判结束。
- 幂等：恢复路径也要考虑 client_message_id 去重（复用现有 IDEMPOTENCY_INDEX 语义或按 task_id 去重）。
- 事件时序：`task_waiting_user` 之后不能再有 `task_completed`；恢复后的事件继续用同一 task_id。

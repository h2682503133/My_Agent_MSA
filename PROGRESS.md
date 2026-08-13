# 工作进度 — 询问挂起/恢复（pause & resume）

> 实现记录（2026-08-13）。设计草案的历史内容已由本次覆盖。

## 一、目标（已实现）

模型输出 `询问:xxx` 时，任务进入挂起状态，不结束：

1. 挂起任务继续占着该用户队列的位置（用户回复时找回它继续跑），但释放执行槽位。
2. 用户回复后从挂起处恢复：回复内容弹栈拼接（与「对话：」指令一致），agent 继续执行。
3. 超时（1 小时）不删除任务：由系统替用户回复一条提示，让模型自行判断。

## 二、已拍板的决策

| 问题 | 决策 |
|---|---|
| 挂起期间用户发新消息 | 视为对挂起任务的回复，直接恢复，不排队 |
| 恢复后是否继续占位 | 是，挂起任务恢复运行期间用户仍 busy |
| 超时 TTL | 1 小时；到点后系统替用户回复「用户未在规定时间内回复，请根据情况自行判断」，走正常恢复流程 |
| 询问的语义 | 与「对话：」指令类似：询问内容压栈，用户回复后同样弹栈拼接 |
| 并发/槽位语义 | 挂起用户保留 BUSY（阻滞其后续任务），但并发上限按「BATCH_SIZE + 挂起用户数」判断，挂起不占真实并发额度 |
| 询问消息格式 | 发给用户的消息必须带「询问：」前缀 |
| 最终回复引用 | 仍引用最初那条任务消息（原始 client_message_id） |

## 三、实现改动

### orchestrator（agent-orchestrator-service）
- `proto/agent_orchestrator.proto`：新增 `ResumeTaskRequest` + `rpc ResumeTask`。
- `app/agent_runtime.py`：
  - `PENDING_TASKS` 内存注册表（task_id -> TaskRuntime），`suspend_task()` 登记并发射 `task_waiting_user`（文本带「询问：」前缀）。
  - `send()` 的 `询问:` 分支改为：压栈 `{from: 询问方, input: "【已发送给用户】\n询问：xxx"}`、置 `status="suspended"`、不弹栈不补终态。
  - `process_task` 拆出 `_continue_task`（主循环+收尾）；`status=="suspended"` 时不发 `task_completed`。
  - `resume_task()`：弹出挂起时压的上下文，把回复（用户回复或系统超时提示）作为「收到返回」拼接，交给询问方继续 send，再走 `_continue_task`。
- `app/server.py`：抽取 `_stream_task_events`；新增 `ResumeTask` handler（找不到挂起任务时回退为新 ExecuteTask）。

### scheduler（task-scheduler-service）
- `app/config.py`：`SCHEDULER_SUSPEND_TTL_SECONDS`（默认 3600）、`SCHEDULER_SUSPEND_SWEEP_INTERVAL`（默认 30）。
- `app/scheduler.py`：
  - `SUSPENDED_TASKS`（user_id -> (ScheduledTask, suspend_at)）+ `SUSPENDED_LOCK`。
  - 并发判断改为 `processed >= BATCH_SIZE + 挂起数`（`_concurrency_limit`）。
  - `_run_execution`：收到 `task_waiting_user` 时登记挂起、释放物理槽位、保留 BUSY_USERS/processed、不补终态事件。
  - `submit_task`：该用户有挂起任务时，新消息走恢复路由（不排队）。
  - `_run_resume`：原子占位（`_claim_suspended`）后等待空闲槽位，构造 resume ScheduledTask 调 orchestrator `ResumeTask`；结束后正常释放槽位/BUSY/processed。
  - `_suspend_ttl_sweeper`：扫描超时挂起任务，用系统消息替用户回复并恢复。
- `app/orchestrator_client.py`：新增 `resume_task()` gRPC 调用。

### 前端 / gateway
- 无需改动：`task_waiting_user` 事件展示已有，消息文本带「询问：」前缀直接展示。

## 四、验证情况

- 本地 `ast.parse` 语法校验通过。
- 本地逻辑级测试通过：挂起登记/槽位释放/BUSY 保留、恢复路由不排队、并发上限=设定值+挂起数、TTL 超时系统替用户回复。
- 构建部署：需要重新 build agent-orchestrator-service / task-scheduler-service 镜像并 apply 对应 yaml（Docker 构建时自动重生成 proto）。

## 五、注意点

- 挂起状态在 orchestrator 内存里，orchestrator 重启后 `ResumeTask` 找不到挂起任务，会回退为新任务（接受）。
- 挂起期间用户在其他会话发消息也会被视为对该挂起任务的回复（按用户决策：一律视为回复）。
- 多轮询问天然支持：恢复后再次 `询问:` 会再次挂起并重新登记同一 task_id。

## 六、修复记录（2026-08-13）

- **QQ 收不到询问内容**：qq-llbot-service 事件循环原来只处理 `assistant_message` 与终态事件；新增 `task_waiting_user` 分支推送「询问：xxx」（v3）。不设「需要你补充信息」兜底，text 为空时发送「出现某些问题，询问无法发送，请重新发送」。
- **「请问/请询问:」被误判为询问**：`syntax_parser._find_question_tail` 原来用 `full_text.find("询问:")` 任意位置匹配，`请询问:`、`我询问:`、`想询问:` 等自然语言会被误判；改为边界正则 `(?<![一-龥A-Za-z0-9])询问\s*:`，只有独立「询问:」指令才生效（v36）。
- **询问内容为空**：orchestrator 询问分支不再兜底「请补充必要信息后我再继续。」；question 为空时把「出现某些问题，询问无法发送，请重新发送」推回模型重新生成（最多重试 2 次，仍失败则发该提示给用户结束）。

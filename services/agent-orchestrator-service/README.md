# agent-orchestrator-service

这是从原 `My_Agent/core/Agent` 拆出的智能体编排服务。

它对应你的微服务架构中的：

```text
agent-orchestrator-service
```

职责：

```text
1. 接收 task-scheduler-service 的 ExecuteTaskRequest
2. 构造 TaskRuntime
3. 保留原 Task.py 中的压栈 / 弹栈运行时语义
4. 加载 agent_list.json 和 system_prompt
5. 调用 openviking-context-service 获取上下文
6. 调用 model-proxy-service 获取模型输出
7. 解析 工具调用 / 对话 / 询问 / 切换 / 定时任务 指令
8. 调用 tool-runtime-service 执行工具或技能
9. 以 TaskEvent stream 形式返回 scheduler
```

模块划分：

```text
app/agent_runtime.py     模型调用、响应解析、工具与智能体路由（主流程）
app/context_builder.py   上下文拼接：system prompt 构建 / messages 组装 / 世界书注入 / TOOL 注入
app/task_runtime.py      TaskRuntime（上下文栈）
app/context_client.py    持久化上下文（openviking-context-service）
```

## 和 task-scheduler-service 的边界

`scheduler` 只保存轻量任务对象：

```text
ScheduledTask / TaskDTO
```

`orchestrator` 收到 `ExecuteTaskRequest` 后才构造真正运行时对象：

```text
TaskRuntime
```

也就是说，原来的 `Task.py` 里面这些内容属于 orchestrator：

```text
agent_context
push_context()
pop_context()
temp_dialog_input
temp_dialog_output
main_memory
tool_log
main_log
pending task
```

## 端口

默认监听：

```text
0.0.0.0:5300
```

对应 scheduler 的：

```text
ORCHESTRATOR_TARGET=agent-orchestrator-service:5300
```

## 本地运行

```bash
pip install -r requirements.txt
bash scripts/gen_proto.sh
python -m app.main
```

## 下游服务地址

默认配置：

```text
OPENVIKING_CONTEXT_TARGET=openviking-context-service:5301
MODEL_PROXY_TARGET=model-proxy-service:5302
TOOL_RUNTIME_TARGET=tool-runtime-service:5303
```

如果这些下游服务暂时没有完成，任务会直接失败并暴露错误，避免误以为真实模型已经调用成功（见下文 no-mock build）。

## K8s

版本号只维护在 `deploy/services/agent-orchestrator-service.yaml` 的 `image` 字段（`deploy-all.ps1` 会从那里解析 tag 并据此构建）：

```bash
docker build -t agent/agent-orchestrator-service:v77 services/agent-orchestrator-service
kubectl apply -f deploy/services/agent-orchestrator-service.yaml     # 或 kubectl -n agent rollout restart deployment/agent-orchestrator-service
```

## 当前说明

这个版本优先完成服务边界拆分：

```text
scheduler → orchestrator：gRPC streaming ExecuteTask
orchestrator → scheduler：TaskEvent stream
orchestrator 内部：TaskRuntime 压栈 / 弹栈
```

真正的 `openviking-context-service`、`model-proxy-service`、`tool-runtime-service` 可以后续继续拆。


## PV 挂载 config 和 system_prompt

现在 Deployment 会把 PVC 挂载到：

```text
/app/config
/app/system_prompt
```

对应环境变量：

```text
AGENT_CONFIG_PATH=/app/config/agent_list.json
SYSTEM_PROMPT_DIR=/app/system_prompt
```

首次部署顺序：

```bash
kubectl apply -f deploy/services/agent-orchestrator-service.yaml
```

依赖 `my-agent-config-pvc`（由 `deploy/apply-pv.sh` 创建），通过 subPath 挂载：

```text
orchestrator/config       → /app/config          （agent_list.json、system_settings.json、process）
orchestrator/system_prompt → /app/system_prompt   （各智能体 System Prompt）
```

## 工具清单（system_prompt/TOOL.json）

工具清单统一放在 `SYSTEM_PROMPT_DIR/TOOL.json`，各智能体的 `TOOL.md` 只写注入选项（正文可选，作为该智能体补充说明追加）：

````markdown
```tool-inject
{"sections": ["*"], "tools": {"include": ["*"], "exclude": ["codex"]}}
```
````

- `sections`：段 id 列表（`"*"` 全部，`"!段id"` 排除）
- `tools`：`{"include": [...], "exclude": [...]}`，token 可写工具名 / 段 id / `groups` 标签
- 目录里 `"enabled": false` 的段或工具默认不注入，被选项点名才注入
- 没有选项块的老式 `TOOL.md` → 整份原文照旧注入；`TOOL.json` 缺失 → 只注入 `TOOL.md` 正文
- 细节见仓库根目录 `上下文管理.md` 第 7 节

## 提示词文件开头的指令区（`@ref=` / `@when=`）

一个提示词文件的**开头**可以是指令区（连续的「指令行或空行」，出现第一行正文即结束）。
指令行**不进入注入内容**，读取期就被摘掉，下游只看到正文。两条指令都只允许写一行：

````markdown
@when=single
@ref=alice

（这里是正文）
````

| 指令 | 语义 |
|------|------|
| `@ref=<agent_id>` | **读取期**把整份内容替换成「另一个智能体的**同名**文件」。整文件替换语义 → 写了它就不能再有正文 |
| `@when=group` / `@when=single` | 该段**只在群聊** / **只在非群聊**注入 |

规则：

- 指令只能出现在指令区；指令区之后（正文里）再出现 `@ref=` / `@when=` → 报错，避免误写被静默吞掉
- `@ref=`：目标 agent 目录 / 同名文件不存在、内容为空、指向自己、目标内部再含指令（只允许一层）→ 报错
- `@when=`：取值只允许 `group` / `single`（大小写与 `=` 两侧空格容错）；目标文件内部含 `@when=` → 报错
  （注入条件属于**写它的那个 agent**，不随内容共享）
- 支持范围：`files` 里声明的普通 `.md` 段 + `GLOBAL_SETTING.md`。
  `process*.md`（内容不读）/ `world_info.md`（内容只作阶段1 指令）/ `TOOL.md` 写了 `@when=` → 报错
- `@when=` 的判定是**发送期**按 `task.user.session_id` / `task.user.id` 是否以 `grp_` / `qq_g_` 开头
  （`context_builder.is_group_session`，与 openviking-context-service 的会话共享口径一致）。
  之所以不在构建期判定：system prompt 在实例创建时构建并被 LRU 缓存复用，而"是否群聊"是**每次请求**的属性
- 失败一律抛 `PromptRefError`，不做静默降级

**典型用法**：群聊时不要注入用户信息 —— 在 `USER.md` 首行加 `@when=single`。

自测：`python tools/prompt_refs_smoke_test.py`（临时目录造数据，覆盖正常路径与全部报错分支）。

## 历史块的 role 重分配（短期 session 记忆）

历史对话块在**加载期**按说话人重新分配 `role`，实现在
`context_builder.shape_history_messages`，只作用于对话历史那一段。

**问题**：群聊共享 session 里**所有角色**的发言在存储层都是 `role="assistant"`
（openviking-context-service 的 `viking_store.append_turn` 对每个 agent 一视同仁），
而 `assistant` 通道的语义是"这是我说的"。直接透传的后果是模型把别人的话当作自己说过的
话，并从「assistant 通道里装着各种 `<名字>:` 开头的角色散文」这个格式模板里推断出
"替别的角色写台词也是我的职责"。实测：98 个 assistant 回合里 7 个出现冒充
（记录作者 ≠ 正文里写的说话人）。

**为什么修在加载期而不是存储期**：`assistant` 这个 role 是**相对观察者**的 ——
同一条 lilith 的发言，对 lilith 是 `assistant`，对 alice 必须是别的通道，一条物理记录
无法同时表达两者。存储层只存观察者无关的事实（"这条是 lilith 产生的"），而这个事实
**已经存了**（`role_id`/`peer_id` + `<agent_id>:` 前缀）。

**为什么解析 `<name>:` 前缀就够**：写入时每条 agent 发言都必然带 `<agent_id>:` 前缀，
实测 8 个会话文件、全部记录里「外层前缀 != peer_id」出现 **0 次** —— 前缀是存储层写的，
恒等于真实作者。所以解析前缀等价于读作者字段，**不需要改 proto、不需要动存储服务**。

| 情况 | 处理 |
|------|------|
| 说话人 == 自身 `agent_id` | 保留 `assistant`（自己的语气连续性是资产） |
| 说话人 != 自身，原 role 是 `assistant` | 挪到 `user` 侧，**连续段合并**成一条 |
| 原 role 已是 `user`（用户消息 / `<agent_e>:` 调度转场） | 原样保留，不参与合并 |
| 无说话人标记（`工具摘要：…`） | 原样保留，不参与合并 |
| `【会话摘要】` / `【相关记忆】` | 原样透传（不是对话历史） |

- **只重排 role 与合并，不改动任何正文**（`<name>:` 标记保留，与 `GLOBAL_SETTING.md` 的
  `<ID>` 约定同源）
- 单角色私聊**天然退化为恒等变换**（那里的历史不是自己的就是用户的），所以私聊路径零回归
- 历史块上方的免责框 `HISTORY_DISCLAIMER` 作用范围是**它下面的整个历史块**，
  且带优先级裁决（与上方系统设定矛盾时以系统设定为准）

自测：

```bash
python tools/history_shape_smoke_test.py          # 37 项，含私聊恒等与全部边界
# 实时检查（集群内跑，验收标准是"错通道清零"，改造前基线 31/66）
kubectl -n agent cp tools/history_shape_live_check.py <pod>:/tmp/check.py
kubectl -n agent exec <pod> -- python /tmp/check.py
```

## agent_list.json 按用户选择

支持新格式：

```json
{
  "default": {
    "main": {
      "model_profile": "default-main",
      "files": ["SOUL.md"]
    },
    "tool": {
      "model_profile": "default-tool",
      "files": ["SOUL.md", "TOOL.md"]
    },
    "reader": {
      "model_profile": "default-reader",
      "files": ["SOUL.md"]
    }
  },
  "users": {
    "dujiawei": {
      "main": {
        "model_profile": "dujiawei-main",
        "temperature": 0.8
      }
    }
  }
}
```

选择规则：

```text
1. 如果 users 里有该 user_id，则优先使用用户配置。
2. 用户只写了部分字段时，会覆盖 default 对应 agent 的字段。
3. 用户不存在时，使用 default。
4. 旧的平铺格式仍然兼容：{"main": {...}, "tool": {...}, "reader": {...}}
```


## no-mock build

本版本不再提供 `MOCK_DOWNSTREAM` 运行链路。`agent-orchestrator-service` 会直接调用：

```text
openviking-context-service:5301
model-proxy-service:5302
tool-runtime-service:5303
```

如果下游服务不可用，任务应直接失败并暴露错误，避免误以为真实模型已经调用成功。

## 长期记忆降级（openviking-context-service 不可用时）

当 `openviking-context-service` 未部署或不可达时，上下文检索自动降级为本地维护的最近对话（`app/local_context.py`）：

- 每个 `(user_id, session_id, agent_id)` 维护最近 **4 个回合**（用户消息 + 智能体回复算 1 条）。
- 存储于 config 卷（`PROCESS_DIR` 同级 `local_context/` 目录），JSON 文件形式，重启后可延续。
- `SearchContext` 失败 → 返回本地最近回合；本地无历史时返回「长期记忆服务未启用」提示。
- `AppendTurn` 失败 → 写入本地最近回合，对话连续性不受影响。

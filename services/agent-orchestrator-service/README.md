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

另外还会挂 `my-agent-workspace-pvc` 到 `/app/workspace`（`WORKSPACE_DIR=/app/workspace`），
与 tool-runtime 共用同一个用户工作区：

- 带图消息的图片由 orchestrator **自动落盘**（`app/image_store.py`）：
  `<用户工作区>/uploads/images/<YYYYMMDD>/<sha1 前16位>.<ext>`，同图按内容哈希去重；
- 提示词（main 的「本次请求对话」）与本轮 openviking 历史里都会带
  `（附图: uploads/images/…）`，模型下一轮可据此调 tool-runtime 的 `image-view` 回看；
- 落盘失败只记日志、不影响本轮对话（失败闭合）。

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
| `@ref=<目标>` | **读取期**把整份内容替换成目标文件。整文件替换语义 → 写了它就不能再有正文 |
| `@when=group` / `@when=single` | 该段**只在群聊** / **只在非群聊**注入 |

`@ref=` 的目标有三种写法：

| 写法 | 含义 | 例子 |
|------|------|------|
| 含 `/` | 提示词目录内的**相对路径**（文件名可任意） | `@ref=人设合集/凌伊.md`、`@ref=alice/SOUL.md` |
| 不含 `/`、末段带 `.md` / `.json` | **根目录下的文件** | `@ref=人设甲.md` |
| 其余（老写法） | 另一个目录下的**同名文件** | `lilith/SOUL.md` 里写 `@ref=alice` → `alice/SOUL.md` |

规则：

- 指令只能出现在指令区；指令区之后（正文里）再出现 `@ref=` / `@when=` → 报错，避免误写被静默吞掉
- `@ref=` 目标只允许**提示词目录内的相对路径**：绝对路径 / 盘符 / `\` / `..` / 空段
  （`a//b`、结尾多写 `/`）一律报错；也不能用 `{占位符}` 当目标。中文目录名允许
- `@ref=` **最多 3 跳**（`prompt_refs.MAX_REF_DEPTH`）。超限报错并打印**完整链**：
  `@ref= 跳转超过 3 跳｜链：lilith/SOUL.md → alice/SOUL.md → 人设合集/凌伊.md → …`。
  **刻意不做环检测**：成环必然在某一跳撞上上限，而链打出来就能看出是环
- `@ref=`：目标目录 / 文件不存在、内容为空、指向自己 → 报错
- 目标文件里**不能**带 `@when=`（任何跳数下都成立）：注入条件属于**写它的那个 agent**，
  不随内容共享。目标文件里再写 `@ref=` 是允许的（就是上面那个"最多 3 跳"）
- 链上的 `{agent_id}` 永远取**最外层请求方**：`lilith → alice → 人设文件` 里，
  人设文件写 `你是 {agent_id}` 得到 `lilith`
- `@when=`：取值只允许 `group` / `single`（大小写与 `=` 两侧空格容错）
- 支持范围：`files` 里声明的普通 `.md` 段 + `GLOBAL_SETTING.md`。
  `process*.md`（内容不读）/ `world_info.md`（内容只作阶段1 指令）/ `TOOL.md` 写了 `@when=` → 报错
- `@when=` 的判定是**发送期**按 `task.user.session_id` / `task.user.id` 是否以 `grp_` / `qq_g_` 开头
  （`context_builder.is_group_session`，与 openviking-context-service 的会话共享口径一致）。
  之所以不在构建期判定：system prompt 在实例创建时构建并被 LRU 缓存复用，而"是否群聊"是**每次请求**的属性
- 失败一律抛 `PromptRefError`，不做静默降级

**典型用法**：

- 群聊时不要注入用户信息 —— 在 `USER.md` 首行加 `@when=single`
- 一份内容给多个智能体共用 —— 角色目录里放一行指针，正文只维护一份：

  ```
  alice/SOUL.md          ← 人设正文（真身）
  lilith/SOUL.md         ← @ref=alice
  luna/SOUL.md           ← @ref=alice
  ```
- **几套人设快速切换** —— 让 hub 再指向人设文件（两跳），换一行 = 整个班底换人设：

  ```
  人设合集/凌伊.md                 ← 一套人设，一个文件
  alice/SOUL.md = @ref=人设合集/凌伊.md
  lilith/SOUL.md = @ref=alice      ← 这些存根一个字都不用改
  luna/SOUL.md   = @ref=alice
  ```

  注意**指针方向**：不要让存根直接指人设文件（那是 5~15 处要改，而且漏改一个
  不报错 —— 它会继续用 hub 的旧内容，属于静默的部分切换）。

自测：`python tools/prompt_refs_smoke_test.py`（临时目录造数据，覆盖三种目标写法、
深度上限、成环、路径穿越与全部报错分支）。

## 提示词热重载：改完不用重启

`AgentRuntime.send()` 开头比对一次提示词快照（`context_builder.prompt_revision`：
把提示词目录下所有文件的 `(相对路径, mtime, 大小)` 排序成元组，外加
`system_settings.json`；只 stat 不读内容，几十个文件的代价可忽略），变了就重建：

- 改任何一个 `.md` / 增删文件 / **改 `@ref=` 指针行** / **改被引用的目标文件** /
  **改链中间的 hub** → **下一次模型调用即生效**
- 不需要重启 orchestrator（重启会杀掉在飞任务），也不需要等 LRU 把实例挤掉
- `.bak` / `.tmp` 不参与快照（dashboard 备份、原子写中间态不该触发重建）

为什么需要这一步：system prompt 是**实例级**的 —— 实例创建时构建一次就被
LRU 复用（见 `上下文管理.md` 第 8 节）。没有热重载时，改完提示词只有两种情况
会生效：LRU 把实例挤掉（**不同用户可能同时跑着两套人设**），或者重启。

三个实现细节：

- 构建结果整体放在一个属性里（`_prompt_parts`），重建是**一次赋值**，
  不会出现「新 prompt 配旧 `@when` 下标」的中间态（`system_prompt` 等四个名字
  改成了派生 property，调用方无感）
- 刷新点在 `send()` 而不是任务入口：子智能体（tool / reader…）各有自己的实例，
  只刷 main 的话它们的提示词仍是旧的
- 快照是**全局**的（不按 agent 记依赖）：改任何提示词文件都会让所有实例在
  下一次 send 时重建。代价是几个小文件的读取，换来的是"改了 hub 一行，
  5 个角色下一条消息全体换"这种零心智负担的行为

自测：`python tools/prompt_hot_reload_smoke_test.py`（覆盖快照判定、改正文 /
改指针 / 改目标文件 / 改中间 hub、`.bak` 不误触发、失败不静默、真实实例缓存）。

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
- **唯一例外：伪指令行**。`→tool: …` / `←main: …` 这类箭头写法从来没被解析执行过
  （`_find_command_block` 不认箭头），留着会教模型把它当"转交指令"复读——实测自锁过一次。
  注入前在 assistant 侧整行剔除（库里原文不动），整条被清空的消息一并丢弃。
  成因与三道防线见仓库根 `上下文管理.md` §6.1
- 单角色私聊**天然退化为恒等变换**（那里的历史不是自己的就是用户的），所以私聊路径零回归
- 历史块上方的免责框 `HISTORY_DISCLAIMER` 作用范围是**它下面的整个历史块**，
  且带优先级裁决（与上方系统设定矛盾时以系统设定为准）

自测：

```bash
python tools/history_shape_smoke_test.py          # 37 项，含私聊恒等与全部边界
python tools/directive_guard_smoke_test.py        # 28 项，伪指令渲染/清洗/告警
python tools/frame_kind_smoke_test.py             # 25 项，帧类型（kind）与「收到返回」判据
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
  （注意：本地降级存储**不加**说话人前缀，扇出时正文已带前缀所以天然正确。）

## 结构化调用（文字 RPG 的外部驱动协议）

支持把**用户输入整体当成一段 JSON** 来驱动一次调用（外部程序当游戏引擎、agent 当 NPC）。
**普通聊天完全不受影响**：判定不通过就完全不解析，`task` 一个字段都不动。
完整设计见仓库根 `上下文管理.md` 第 9 节；代码在 `app/prompt_plan.py`。

最小形态（只有 `text` 必填）：

```json
{
  "text": "玩家推开了门",
  "exclude_block": ["tool"],
  "insert": [{"at": "before_input", "content": ["HP=10，位置：酒馆"]}],
  "present": ["alice", "bob"]
}
```

| 键 | 含义 |
|---|---|
| `text` | **唯一必填**。解析后取代 `task.content` —— 模型与历史都只看到它 |
| `exclude_block` | 只关不开，可选 `tool` / `world_info` / `memory` / `openviking`（`openviking` 只停注入、不停写入，且连检索都不发） |
| `insert` | `{"at": 锚点, "content": ["…", "…"]}`，按顺序以 `system` 消息插入。锚点：`before_persona` / `before_history` / `before_input` / `after_input` |
| `present` | 本次在场 NPC；收尾时逐人各写一份历史（正文首部写真实发言者） |

失败语义：

- JSON 解析不了 / 不以 `{` 开头 / 没有已知键 → **当普通内容**（不解析、不报错）
- 有已知键但字段不合法 → `task_failed` 事件 + 原文前 300 字（显式，不被 raw fallback 吞）

## 模式开关（`模式:` 指令）

在**不换智能体**的前提下，切换某个会话注入的**提示词**与**模型**——`agent_id` 不变，
所以会话 key 不变，上下文天然共用（`切换:` 是换 agent，历史和会话都会跟着走）。

### 指令

| 指令（由模型在回复末尾输出） | 语义 |
|---|---|
| `模式:special` | 切到 `special` 模式 |
| `模式:`（空） | 回到 `default` 模式 |
| `切换:<agent>` | 换智能体，**同时把模式重置为 `default`** |

变量存在 `AgentRuntime.default_mode`（与 `default_agent` 并列，按 `session_id` 记，
进程内、重启即空；缺省为 `default`）。

### 模式定义（`agent_list.json`，每个 agent 一份）

```json
"main": {
  "model_profile": "default-main",
  "files": ["SOUL.md", "TOOL.md", "IDENTITY.md"],
  "modes": {
    "special": {
      "files": ["SOUL_special.md", "IDENTITY.md"],
      "model_profile": "default-talker",
      "temperature": 1.2
    }
  }
}
```

- 可覆盖 `files` / `model_profile` / `temperature` / `max_tokens`（浅合并，模式优先）；
- 没写的键沿用 agent 级配置；
- `default` 模式 = agent 原配置，不定义 `modes` 时行为与改造前完全一致。

### 生效与失败处理

- 指令解析后立即重建 system prompt（`refresh_system_prompt` 会比较模式名，不一致就重建）；
- 模式名未定义 → 回复`【模式切换失败】未知模式 'x'，可用模式：default, special`；
- 模式声明的提示词文件缺失 → 回复里列出缺失文件名（不静默少注入）；
- 空 `模式:` 永远合法（等价于 `default`）。

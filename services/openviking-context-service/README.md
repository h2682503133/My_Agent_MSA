# openviking-context-service

这是从原 `My_Agent` 中拆出的 OpenViking 上下文服务。

它对应你的微服务架构中的：

```text
openviking-context-service
```

职责：

```text
1. 管理 viking_data 持久化目录
2. 为 agent-orchestrator-service 提供会话上下文检索
3. 为 agent-orchestrator-service 提供会话追加记录
4. 为 tool-runtime-service 提供技能文档导入
5. 为 agent-orchestrator-service 提供技能文档检索
```

## 原代码迁移依据

原 `core/Agent/Agent.py` 中：

```python
GLOBAL_VIKING_CLIENT = ov.OpenViking(path="./viking_data")
GLOBAL_VIKING_CLIENT.initialize()
```

以及 `Agent.__init__()` 中：

```python
self.ov_client = GLOBAL_VIKING_CLIENT
self.ov_session = self.ov_client.session(session_id=f"{self.id}_{self.session_id}")
self._load_viking_session()
```

和 `get_context_sync()` / `add_message()` 逻辑，现在迁移到本服务。

原 `core/Agent/Skill_manager.py` 中：

```python
self.client = ov.SyncOpenViking(path=data_path)
self.client.initialize()
self.client.add_skill(str(skill_md_path), wait=True)
self.client.read("viking://agent/skills/...")
self.client.ls("viking://agent/skills/")
```

现在迁移到本服务的技能文档接口。

## 端口

默认监听：

```text
0.0.0.0:5301
```

K8s Service：

```text
openviking-context-service:5301
```

## gRPC 接口

```text
SearchContext
AppendTurn
SearchSkillDocs
AddSkillDocument
ListSkillDocs
ReadSkillDoc
```

## 本地运行

```bash
pip install -r requirements.txt
bash scripts/gen_proto.sh
python -m app.main
```

如果本地没有安装 openviking，可以先用 mock 模式跑通链路：

```bash
export MOCK_VIKING=true
python -m app.main
```

## Docker

```bash
docker build -t agent/openviking-context-service:v1.2 .
```

## K8s

```bash
kubectl apply -f deploy/services/openviking-context-service.yaml
```

部署要点：

- 数据与索引由 `openviking-server` 管理（`deploy/services/openviking-server.yaml`），本服务通过 `OPENVIKING_BACKEND=server` + `OPENVIKING_SERVER_URL=http://openviking.agent.svc.cluster.local:1933` 连接。
- API key 从 config 卷读取：`OPENVIKING_API_KEY=/app/config/openviking/api_key`、`OPENVIKING_ROOT_API_KEY=/app/config/openviking/root_api_key`。
- 依赖 `my-agent-config-pvc`（挂载到 `/app/config`，只读）。

## assistant 正文的说话人前缀（与 orchestrator 的契约）

存储层给 assistant 正文定归属时，规则是**以文本内容为准**：

- 正文首部**已有** `<名字>:`（`^[ \t]*<[^>\n]{1,64}>[ \t]*[:：]`）→ **原样存储**，不再补前缀
- 否则 = 改造前的旧行为：先剥掉"自己"的前缀（`<id>:` / `id:` / `<id>：` / `id：` 四种写法）
  并 `strip`，再统一补一次 `<agent_id>: `

为什么要"文本为准"：多 agent **扇出写入**时，`agent_id` 是**收件人**（要拿去拼 session key），
真实发言者由调用方写在正文首部。若无脑再补一次，正文会变成 `<收件人>: <发言者>: …`，
而读取侧（orchestrator 的 `context_builder.history_speaker`）只取**最外层**前缀 →
每个收件人都把别人的话当成自己说的。

开关：`AppendTurnRequest.metadata["author_in_content"]`（真值集合
`true/1/yes/on/是/开`）。**只有 orchestrator 的扇出路径会带**，其余场景 metadata 为空 →
走旧分支，私聊与群聊行为一字未变。

⚠️ 键名必须与 orchestrator 的 `app/context_client.AUTHOR_IN_CONTENT_KEY` 完全一致
（`tools/prompt_plan_fanout_smoke_test.py` 里有跨服务比对）。

自测：`python tools/assistant_prefix_smoke_test.py`（33 项，含与
`context_builder.history_speaker` 的**正则口径比对** —— 两边漂移会导致"存的时候跳过了
自动前缀、读的时候解析不出说话人"的静默错配）。

## 会话历史：时间戳与"未 commit 内容"的召回

### 1. 时间戳

OpenViking 的 session 消息本来就带 `created_at`（ISO8601，UTC），之前被丢掉了。
现在整条链路透传，并在注入模型前渲染成本地时间前缀：

```text
proto ChatMessage.created_at
  → viking_store._message_created_at
  → server.SearchContext
  → context_client.search_context
  → context_builder.history_time_stamp / shape_history_messages
```

渲染结果形如 `[10-26 13:04] <2195239690>: 我的猫粮快没了…`；老数据没有 `created_at`
则不加前缀（正文原样）。`【会话摘要】/【相关记忆】` 不是对话，不加时间戳。

### 2. 未 commit 会话内容的向量召回（`app/session_recall.py`）

**背景（实测）**：OpenViking 只对 `memories / resource / skill` 建向量索引
（`find` 的 `context_type` 仅这几种），会话的 `messages.jsonl` **不建索引** ——
在 session 上 `find` 恒 0 命中。而 `commit_limit`（默认 15 条）才压缩进长期记忆、
上下文窗口只取最近 `max_messages`（默认 6）条，中间的几条既不在窗口里、也没进长期记忆。

**做法**：本地补一层召回，不写任何存储：

1. `client.read("viking://user/<uid>/sessions/<sid>/messages.jsonl")` 取未 commit 的全部消息
2. 按条数从尾部剔除"已经在窗口里"的最新 N 条（尾部对不上时才退回按正文去重）
3. 用 embedding 把 query 与候选消息向量化（候选按内容哈希缓存，进程内上限 1024）
4. 余弦相似度取 top-k（阈值过滤 + 同文去重）

**embedding 走哪里**：只调 `embedding-service`（gRPC `EmbeddingService.Embed`，唯一收束口）。
本服务不硬编码 ollama / OpenAI 地址；provider 由 embedding-service 配置决定。
embedding 失败/超时只记一条日志并跳过召回，不影响本轮对话。

**缓存**：向量缓存**按 `user_id` 分桶**，每桶独立容量与 FIFO 淘汰
（`SESSION_RECALL_CACHE_PER_USER`，默认 256 条），桶数超上限按 LRU 整桶淘汰
（`SESSION_RECALL_CACHE_MAX_USERS`，默认 64）。key 含模型名，换模型自动失效。
进程内缓存，重启即空。

**配置**：

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `SESSION_RECALL_ENABLED` | `true` | 总开关 |
| `SESSION_RECALL_TOP_K` | `3` | 最多注入几条 |
| `SESSION_RECALL_MIN_SCORE` | `0.45` | 余弦阈值 |
| `SESSION_RECALL_MAX_MESSAGES` | `200` | 参与打分的候选上限（取最近 N 条） |
| `EMBEDDING_SERVICE_TARGET` | `embedding-service:5303` | 向量计算入口（gRPC） |
| `EMBEDDING_MODEL` | `qwen3-embedding:0.6b` | 请求模型（需与索引侧一致，1024 维） |
| `SESSION_RECALL_CACHE_PER_USER` | `256` | 每用户缓存条数上限 |
| `SESSION_RECALL_CACHE_MAX_USERS` | `64` | 最多保留多少个用户的缓存桶 |

命中结果以 `memory_id="session:<msg_id>"` 的形式并入 `memories`（与长期记忆同一注入通道，
编排层渲染成 `【相关记忆】[10-26 13:04] …`），总条数仍受 `MAX_MEMORY_HITS`（默认 5）约束。

### 3. 记忆组装（`app/memory_assembly.py`）

**为什么单独一个文件**：`viking_store.py` 只负责跟 OpenViking 说话（发请求、收命中），
"哪几条该注入、按什么顺序、摘要还是全文、草稿能不能用"是**策略**，会随接入 OpenViking 的
类型/状态/时间而膨胀；拆出来才能离线单测、也方便以后按类型分层。
模块本身不联网、不读配置、不打印。

**候选来源与优先级**（同类型同分时的顺序，也是老数据的兜底顺序）：

| source | 含义 |
|---|---|
| `archive` | 会话归档摘要（`pre_archive_abstracts`），优先级最高 |
| `session` | 本会话 `find` 命中 |
| `global` | 全库 `search` 兜底命中 |
| `recall` | 未 commit 消息的本地向量召回（见上一节） |
| `window` | 仅作兜底：全部被裁掉时注入最近一条消息（`score=-1`） |

**排序**：`(记忆类型优先级, 来源优先级, -score)`。类型按 OpenViking 的目录语义排序：
常驻画像（`profile` / `identity` / `soul`）→ `preferences` → `entities/events/cases/patterns`
→ `tools/skills/experiences/trajectories` → `session/resource/unknown`。
类型从命中里的 `memory_type`（含 `metadata.memory_type`）或 URI 目录推断，都没有就是 `unknown`
——此时排序退化成"来源 + 分数"，与改造前行为一致。

**内容**：命中里有 `abstract` 就用它（省 token），其次 `overview`，最后才是全文
（`level` 字段会带出来，便于上游区分）。

**状态**：`status` 为 `deprecated` / `archived` 的默认丢弃；`draft` 保留并在渲染时标成
`[experiences/draft]`（当前集群里 experiences 全是 draft，全丢会等于关掉经验记忆）。

**配置**：

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `MAX_MEMORY_HITS` | `5` | 注入条数上限 |
| `MEMORY_ASSEMBLY_MAX_CHARS` | `0` | 正文字符预算，0=不限；按排序累加到超限为止 |
| `MEMORY_ITEM_MAX_CHARS` | `1200` | **单条上限**（字符）：超了在完整行处截断并标注。OpenViking 侧没有文件级硬上限（profile 实测 17KB），不截断会整段进 prompt |
| `MEMORY_ASSEMBLY_PER_TYPE` | 空 | 每类上限，如 `experiences=2,cases=1`；空=不限制 |
| `MEMORY_DROP_DEAD_STATUSES` | `true` | 是否丢弃 deprecated/archived 的记忆 |

自测：`python tools/memory_assembly_test.py`（45 项：类型推断、命中归一化、阈值/状态/去重/
类型与来源排序、每类上限与预算、兜底、payload 兼容）。

**还没接的 OpenViking 能力**（下一步，按性价比）：
检索侧 `context_type`（`memory`/`resource`/`skill`）、`filter{op:must,field:memory_type}`、
`since/until`（支持 `7d`/`24h` 相对写法）、`include_provenance`、`image_url`（多模态）；
写入/管理侧 `used`（回写本轮用到的记忆）、`content/abstract|overview`、`fs/*`（记忆树浏览）、
`relations`（关系图）、`snapshot`（记忆版本回滚）、`pack`（备份迁移）、
`stats/memories` + `console/*`（用量与审计）、`tasks`/`system/wait`（异步任务状态）。

### 3b. 服务端版本：0.5.3（2026-11 升级）

| 变更 | 说明 |
|---|---|
| 镜像 | `v0.4.10` → **`v0.5.3`**（镜像 `ghcr.io/volcengine/openviking`） |
| 启动 | 0.5.x 镜像默认会拉起 `vikingbot` 网关（失败即整个容器退出）→ 必须 `OPENVIKING_WITH_BOT=0` |
| 配置路径 | 默认变成 `/app/.openviking/ov.conf`；我们仍用 `OPENVIKING_CONFIG_FILE=/etc/openviking/ov.conf`（已验证可用） |
| 客户端 | `0.4.10` → **`0.5.3`**（pip 包 `openviking`）（服务镜像与 mono venv 同步）；配套 `openviking-sdk` 由镜像/依赖解析带入 |
| API | `/api/v1/find`、`/api/v1/search` → `/api/v1/search/find`、`/api/v1/search/search`（**旧客户端会 404**）；`sessions/{id}/extract|used`、`relations`、`code/*` 已移除（我们未使用） |
| WM | 0.5.x **默认不生成** Working Memory（`working_memory.enabled` 默认 false）；我们仍在建会话时显式关，并在 commit 带 `enable_working_memory=False` 给**老会话止血** |
| 参数写法 | 0.5.x 把 `memory_policy`/`score_threshold` 挪进 Options 对象，`commit_session` 的 WM 开关是直接 kwarg —— 代码里三档降级兼容 0.4.x |
| 数据 | 实测：0.5.3 直接在 0.4.10 工作区副本上启动、`search/find` 能召回旧索引、`session/context` 正常；**无需迁移** |
| 其它 | 0.4.10 时代给 `local_collection.py` 打的 `misfire_grace_time` 补丁在 0.5.3 已无必要（`add_job` 已包 try/except） |
### 4. 会话记忆策略（`memory_policy`，`app/config.py`）

**要解决的问题**：OpenViking 每次 commit 默认会生成一份 7 段 Working Memory
（实测 44986 tokens / 142KB，`Errors & Corrections` 一节只增不减），而我们**从不读它**
（`get_session_context` 只有在 overview 装得进 token 预算时才返回，45k 装不下）→ 纯成本：
某个真实 session 157 次 commit 共烧掉 **2116 万 tokens**，其中约 1/3 是这份没人读的 WM。

**做法**：建会话时带 `memory_policy`（OpenViking 只在建会话绑定策略，没有修改接口）：

```json
{
  "working_memory": {"enabled": false},
  "self": {"enabled": true},
  "peer": {"enabled": true},
  "memory_types": ["profile", "preferences", "entities", "events", "experiences"]
}
```

- **关 WM 不影响记忆抽取**：`session.py` 里 `_run_archive_summary()` 只被
  `working_memory.enabled` 门控；`memories/**`（含 `peers/**`）走另一条抽取路径，照常产出；
- `experiences` 属"执行类"，会连带 `cases` / `trajectories`；`tools` / `skills` 不抽；
- **只对新建会话生效**，存量会话（`memory_policy: null`）行为不变——想止血只能新开会话；
- 类型/开关都是环境变量，逃生口是一个完整 JSON。

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `OPENVIKING_WM_ENABLED` | `false` | 是否生成 7 段 Working Memory（关掉省 ~45k tokens/commit） |
| `OPENVIKING_MEMORY_TYPES` | `profile,preferences,entities,events,experiences` | 抽哪些类型（逗号/顿号/空格分隔） |
| `OPENVIKING_SELF_ENABLED` | `true` | `self` 档位（用户自身记忆） |
| `OPENVIKING_PEER_ENABLED` | `true` | `peer` 档位（用户对交互对象的记忆，即 `peers/**`） |
| `OPENVIKING_MEMORY_POLICY` | 空 | 直接给完整 JSON（给了就忽略上面四个）；未知键会被剔除（OpenViking 只认 4 个键） |
| `OPENVIKING_PEER_ID_MODE` | `agent` | 消息的 `peer_id` 取本轮 agent（`off` = 不传，回到改造前） |

**为什么显式传 `peer_id`**：OpenViking 只从**带 `peer_id` 的消息**里抽 `peers/<peer>/` 记忆
（`_message_peer_ids()` → `allowed_peer_ids`；一条都没有就整段跳过）。历史上 2540 条消息只有 9 条带值，
所以 `peers/` 基本不更新。用户级记忆空间下对端就是 agent，因此取 `agent_id`：只影响抽取归属，
**不改变作用域**（还是 `user/<uid>/...`），也不会扩大被别人污染的入口。agent_id 含非法字符
（OpenViking 只接受 `[A-Za-z0-9_.@-]`，不能带 `/`）时宁可不传。

**作用域**：记忆空间是用户级（`viking://user/<uid>/...`），而 `session_id = <agent>_<session>`，
所以 `peers/` 记的是"用户对 main/tool/alice/luna 这些角色的印象"。角色级记忆
（`agent/<id>/...`，即"角色记住用户"）**当前不做**，理由与触发条件见 `docs/长期计划.md` 6.5。

自测：`python tools/memory_policy_test.py`（21 项：policy 形状、类型串解析、环境变量、JSON 逃生口、
ensure_session 的"新建带策略 / 存量不重建 / SDK 不支持时回退"）。

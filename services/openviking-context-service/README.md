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
docker build -t agent/openviking-context-service:v34 .
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

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

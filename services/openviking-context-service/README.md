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
docker build -t agent/openviking-context-service:v1 .
```

## K8s

如果使用 hostPath PV：

```bash
kubectl apply -f k8s/openviking-context-pv-pvc.yaml
kubectl apply -f k8s/openviking-context-service.yaml
```

默认宿主机路径：

```text
/data/my-agent/openviking-context/viking_data
```

注意：如果你是 Docker Desktop / kind 这种节点容器环境，hostPath 目录需要存在于 Pod 所在节点里。

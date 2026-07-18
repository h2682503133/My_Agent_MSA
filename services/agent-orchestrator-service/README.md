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

如果这些下游服务暂时没有完成，可以打开 mock：

```bash
export MOCK_DOWNSTREAM=true
python -m app.main
```

mock 模式下会直接返回一条测试回复，用于先打通 scheduler / gateway / frontend 链路。

## K8s

```bash
kubectl apply -f k8s/agent-orchestrator-service.yaml
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
kubectl apply -f k8s/agent-orchestrator-config-pv-pvc.yaml
kubectl apply -f k8s/agent-orchestrator-config-init-job.yaml
kubectl apply -f k8s/agent-orchestrator-service.yaml
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

# model-proxy-service

这是从原 `core/Agent/Agent.py` 中拆出的模型代理服务。

它对应你的微服务架构中的：

```text
model-proxy-service:5302
```

职责：

```text
1. 接收 agent-orchestrator-service 的 ChatCompletionRequest
2. 根据 model_profile 选择模型配置
3. 调用 OpenAI-compatible / Ollama-like 模型接口
4. 返回统一的 ChatCompletionResponse
```

它不负责：

```text
1. 解析 工具调用: / 对话: / 询问: / 切换:
2. 管理 agent_context
3. 调用工具
4. 管理 OpenViking 上下文
```

这些都属于 `agent-orchestrator-service` 或其他服务。

## 原代码迁移依据

原 `Agent.py` 里直接读取 agent 配置：

```python
api_url = self.config["api_url"]
method = self.config.get("method", "POST")
model = self.config["model"]
api_key = self.config.get("api_key", "")
```

然后直接：

```python
requests.post(api_url, headers=headers, json=json_data, timeout=1500)
```

这部分现在迁移到 `model-proxy-service`。

## 端口

```text
model-proxy-service:5302
```

## 配置文件

默认配置路径：

```text
/app/config/model_profiles.json
```

示例：

```json
{
  "default": "default-main",
  "profiles": {
    "default-main": {
      "provider": "openai_compatible",
      "api_url": "https://api.moonshot.cn/v1/chat/completions",
      "method": "POST",
      "model": "kimi-k2-thinking",
      "api_key_env": "MOONSHOT_API_KEY",
      "temperature": 0.7,
      "model_params": {}
    },
    "default-tool": {
      "provider": "ollama",
      "api_url": "http://model-serving:8000/api/chat",
      "method": "POST",
      "model": "qwen3.5:9b",
      "temperature": 0.7,
      "model_params": {
        "options": {
          "num_ctx": 131072,
          "num_predict": 8192
        }
      }
    }
  }
}
```

`agent-orchestrator-service/config/agent_list.json` 里的 `model_profile` 应该和这里的 profile 名一致。

## 本地运行

```bash
pip install -r requirements.txt
bash scripts/gen_proto.sh
python -m app.main
```

如果暂时没有真实模型服务：

```bash
export MOCK_MODEL=true
python -m app.main
```

## Docker

```bash
docker build -t agent/model-proxy-service:v1 .
```

## K8s

```bash
kubectl apply -f k8s/model-proxy-config.yaml
kubectl apply -f k8s/model-proxy-service.yaml
```

如果使用外部 API Key，建议用 Secret 注入环境变量，例如：

```yaml
- name: MOONSHOT_API_KEY
  valueFrom:
    secretKeyRef:
      name: model-api-keys
      key: moonshot_api_key
```


## v2：模型列表配置

现在优先读取：

```text
/app/config/model_list.json
```

兼容旧的：

```text
/app/config/model_profiles.json
```

推荐格式：

```json
{
  "default": "main",
  "models": {
    "main": {
      "provider": "openai_compatible",
      "api_url": "...",
      "method": "POST",
      "model": "...",
      "api_key_env": "MOONSHOT_API_KEY",
      "temperature": 0.7,
      "model_params": {}
    }
  },
  "aliases": {
    "default-main": "main",
    "dujiawei-main": "main"
  }
}
```

这样 orchestrator 里的 `model_profile: "default-main"` 可以通过 `aliases` 映射到真正的模型配置 `main`。

也兼容原来的 agent_list 风格：

```json
{
  "main": {
    "api_url": "...",
    "method": "POST",
    "model": "...",
    "api_key": "...",
    "temperature": 0.9,
    "model_params": {}
  },
  "tool": {}
}
```

其中 `commit_limit`、`files` 属于 orchestrator，不会传给模型服务。

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

no-mock build：本版本 `MOCK_MODEL` 已硬编码为 `False`，不再提供 mock 模式，模型请求会真实发出，下游不可用时任务直接失败并暴露错误。

## Docker

```bash
docker build -t agent/model-proxy-service:v10 .
```

## K8s

```bash
kubectl apply -f deploy/services/model-proxy-service.yaml
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

## v3：DSML 原生调用标记归一化（2026-10）

部分模型（deepseek-v4-flash）会把训练期的工具调用标记写进 **content 正文**：

    <||DSML|| calls>
    <||DSML|| invoke name="shell">
    <||DSML|| parameter name="command" string="true">ls -la</||DSML|| parameter>
    </||DSML|| invoke>
    </||DSML|| calls>

本系统只认文本协议 工具调用:名字|参数…，orchestrator 不认这套标记。实测两种坏法：
正文只有标记（调用根本没执行、连环重试）；正文"协议行 + 标记"（标记被多行参数吞进
shell 命令，跑坏，还会进历史继续教模型）。

现在在**唯一出口**做归一化，且**不改 `provider_client.py`**：`app/dsml_text.py`（纯函数）+
`app/dsml_adapter.py`（导入即包装 `ProviderClient.chat_completion`，幂等），入口走新文件
`app/bootstrap.py`（Dockerfile CMD 指向它）。

- 正文已有 `工具调用:` 行 → **只删标记**（不重复调用）；
- 正文只有标记 → 把能解析的 invoke **转成协议行**（让调用真的发生）；
- 孤立闭合标签 / 坏块 → 删掉，不留残渣（否则会被多行参数吞进 shell 命令）。
- 竖线兼容 **ASCII | 与全角 ｜（U+FF5C）**：线上真实输出是全角 <｜｜DSML｜｜ …>，只写 ASCII 会整条漏过（踩过）。

orchestrator 不需要知道这件事。自测：`python tools/dsml_text_smoke_test.py`（20 项，含全角竖线与包装后的出口验证）。

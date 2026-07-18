迁移参考：

原 Agent.py 中模型调用逻辑迁移到：
- app/provider_client.py
- app/model_profiles.py

原逻辑：
- 从 agent config 读取 api_url / method / model / api_key / temperature / model_params
- 拼接 messages
- requests.post / requests.get
- OpenAI-compatible 和 Ollama-like 返回结构都做兼容
- 500 重试 1 次

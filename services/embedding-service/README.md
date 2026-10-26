# embedding-service

**向量计算的唯一收束口。** 业务服务（目前是 `openviking-context-service` 的会话召回）
只通过 gRPC 调本服务，不硬编码任何 ollama / OpenAI / 本地模型地址。

## 为什么要单独一个服务

- **换 provider 只改一处**：ollama、vLLM、任意 OpenAI 兼容端点、以后要换成 ONNX 本地模型，
  都只动本服务的配置（或 `app/provider.py`），调用方零改动。
- **统一限流与超时**：批量上限、单条长度上限、超时都在这里兜住，避免每个调用方各写一套。

## 接口（`proto/embedding.proto`）

```proto
service EmbeddingService {
  rpc Ping(PingRequest) returns (PingResponse);   // provider / model / 已知维度
  rpc Embed(EmbedRequest) returns (EmbedResponse); // texts[] → 向量[]
}
```

- `Embed` 空 `texts` → `ok=true`、`items=[]`（当健康检查用）
- `model` 留空 → 用服务默认模型
- provider 失败 → `ok=false` + `error`（不抛异常给调用方，由调用方决定降级）

## 配置

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `EMBEDDING_GRPC_PORT` | `5303` | gRPC 端口 |
| `EMBEDDING_API_BASE` | `http://host.docker.internal:11434/v1/embeddings` | OpenAI 兼容端点 |
| `EMBEDDING_API_KEY` | `ollama` | Bearer token |
| `EMBEDDING_MODEL` | `qwen3-embedding:0.6b` | 默认模型（需与索引侧一致） |
| `EMBEDDING_PROVIDER_NAME` | `openai-compatible` | 只用于上报/日志 |
| `EMBEDDING_TIMEOUT_SECONDS` | `30` | 单次 HTTP 超时 |
| `EMBEDDING_MAX_TEXTS_PER_CALL` | `64` | 超过则拆分多批 |
| `EMBEDDING_MAX_TEXT_CHARS` | `4000` | 单条文本截断长度 |

## 自检

```bash
python tools/provider_test.py     # 分批 / 截断 / 空输入 / 错误传播
```

## 部署

```bash
docker build -t agent/embedding-service:v1 services/embedding-service
kubectl apply -f deploy/services/embedding-service.yaml
```

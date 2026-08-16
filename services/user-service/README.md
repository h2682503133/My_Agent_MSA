# user-service

My_Agent MSA 的用户信息服务：以 JSON 文件存储用户信息、多渠道绑定与 OpenViking API key，同时提供 gRPC 与轻量 HTTP 两种访问方式。

## 端口与命名

```text
namespace: agent
image: agent/user-service:v5
service: user-service
gRPC: 5104
HTTP: 5204
```

## gRPC 接口（proto/user.proto）

| RPC | 说明 |
|-----|------|
| `GetUser` | 获取用户（不存在时自动创建） |
| `UpsertUser` | 更新/合并用户 JSON（`user_json` 为 JSON 字符串） |
| `DeleteUser` | 删除用户 |
| `ListUsers` | 列出所有用户 |
| `BindChannel` | 绑定渠道（channel + channel_user_id + priority） |
| `UnbindChannel` | 解绑渠道 |
| `SetOpenVikingKey` | 存储用户 OpenViking per-user API key |
| `GetOpenVikingKey` | 读取用户 OpenViking API key |

## HTTP 接口（轻量，供 context-service / dashboard 使用）

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/openviking_key/{user_id}` | 查询用户 OpenViking key |
| `POST` | `/openviking_key/{user_id}` | 设置用户 OpenViking key（body: `{"api_key": "..."}`） |

## 数据存储

- 每个用户一个 JSON 文件：`USER_DATA_DIR/{user_id}.json`，包含 `user_id`、`channels`、`created_at`、可选 `openviking_api_key`。
- K8s 中 `USER_DATA_DIR=/data/users`，挂载 `my-agent-user-data-pvc`。

## 环境变量

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `USER_GRPC_HOST` | `0.0.0.0` | gRPC 监听地址 |
| `USER_GRPC_PORT` | `5104` | gRPC 监听端口 |
| `USER_HTTP_PORT` | `5204` | HTTP 监听端口 |
| `USER_DATA_DIR` | `/data/users` | 用户数据目录 |

## 本地启动

```bash
pip install -r requirements.txt
bash scripts/gen_proto.sh          # 生成 app/generated 下的 gRPC 代码
python -m app.main
```

## 构建镜像

```bash
cd user-service
docker build -t agent/user-service:v5 .
```

## 部署

```bash
kubectl apply -f deploy/services/user-service.yaml
```

## 检查

```bash
kubectl get pods -n agent -l app=user-service
kubectl get svc user-service -n agent
kubectl logs -n agent deployment/user-service

# 测 HTTP
kubectl -n agent port-forward svc/user-service 5204:5204
curl http://localhost:5204/openviking_key/test_user
```

## 调用方

- `dashboard-service`：通过 HTTP 取用户 OpenViking key 做长期记忆搜索。
- `openviking-context-service` / 其他服务：通过 gRPC 或 HTTP 存取 per-user key 与渠道绑定信息。

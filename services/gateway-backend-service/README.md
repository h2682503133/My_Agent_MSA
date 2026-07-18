# gateway-backend-service

这是新的 `gateway-backend-service` 后端包，默认直接对接真实 `task-scheduler-service`。

## 端口与命名

```text
namespace: agent
image: agent/gateway-backend-service:local
service: gateway-backend-service
port: 5210
scheduler target: task-scheduler-service.agent.svc.cluster.local:5100
```

## 功能

- `POST /api/login`
- `POST /api/messages`
- `GET /api/events`，SSE
- `GET /api/health`

## 构建镜像

```bash
docker build -t agent/gateway-backend-service:local .
```

## 部署

```bash
kubectl apply -f k8s/gateway-backend-service.yaml
```

## 直接测试

```bash
kubectl -n agent port-forward svc/gateway-backend-service 5210:5210
curl http://localhost:5210/api/health
```

## 前端访问链路

```text
frontend-service
  -> /api/login
  -> /api/messages
  -> /api/events
gateway-backend-service
  -> task-scheduler-service:5100
```

## 注意

这版默认：

```yaml
SCHEDULER_CLIENT_MODE=grpc
```

如果你还想临时回到 mock 模式，可以把环境变量改成：

```yaml
SCHEDULER_CLIENT_MODE=mock
```

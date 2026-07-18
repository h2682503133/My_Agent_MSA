# tool-runtime-service

这是 My_Agent MSA 的 tool-runtime-service 拆分版骨架服务。

## 端口

- gRPC: `5303`
- Kubernetes Service: `tool-runtime-service.agent.svc.cluster.local:5303`
- 同 namespace 内短域名: `tool-runtime-service:5303`

## 当前支持的工具

通过 `ExecuteToolRequest.tool_name` 调用：

- `help`
- `echo`
- `list_workspace` / `list_files` / `ls`
- `read_file`
- `write_file`
- `delete_file`
- `run_shell`：默认关闭，需要 `ENABLE_SHELL_TOOLS=true`

## 构建镜像

```bash
cd tool-runtime-service
docker build --no-cache --progress=plain -t agent/tool-runtime-service:v1 .
```

## 导入 kind 节点

如果是本地 kind / Docker Desktop，并且 YAML 使用 `imagePullPolicy: Never`：

```bash
docker save agent/tool-runtime-service:v1 | docker exec -i desktop-control-plane ctr -n k8s.io images import -
docker save agent/tool-runtime-service:v1 | docker exec -i desktop-worker ctr -n k8s.io images import -
```

## 部署

如果已经有 `my-agent-workspace-pvc`：

```bash
kubectl apply -f k8s/tool-runtime-service.yaml
```

如果 PVC 还没准备好，先用临时无 PVC 版本跑通：

```bash
kubectl apply -f k8s/tool-runtime-service-no-pvc.yaml
```

## 检查

```bash
kubectl get pods -n agent -l app=tool-runtime-service
kubectl get svc tool-runtime-service -n agent
kubectl get endpoints tool-runtime-service -n agent
kubectl logs -n agent deployment/tool-runtime-service
```

## 从 orchestrator Pod 测连接

```bash
kubectl exec -n agent deployment/agent-orchestrator-service -- python -c "import socket; print(socket.getaddrinfo('tool-runtime-service', 5303)); s=socket.create_connection(('tool-runtime-service',5303),5); print('connected'); s.close()"
```

## 备注

这个包是最小可运行版，用于先补齐 `tool-runtime-service:5303` 这条服务链路。
后续如果要支持完整 skill 执行、文件资产上传、容器隔离执行，可以在 `app/server.py` 的 `_dispatch()` 里继续扩展。

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

### Codex（代码生成）

- 调用格式：`codex|工作目录|需求`（或 `codex` + `working_dir`/`requirement` 参数）。
- 通过 SSH 在外部 VM 上执行 `codex exec -C <工作目录> --dangerously-bypass-approvals-and-sandbox --skip-git-repo-check <需求>`。
- 需要环境变量：`CLAW_EXTERNAL_VM_HOST`、`CLAW_EXTERNAL_VM_USER`、`CLAW_EXTERNAL_VM_PORT`、`CLAW_EXTERNAL_VM_SSH_KEY`；VM 上需安装 `@openai/codex` 并完成登录。
- `CODEX_BIN_PATH` 可指定 codex 可执行文件（默认 `codex`）；部署脚本会生成 `/home/<user>/.local/bin/my-agent-codex` 包装器以绕过 SSH 非交互 shell 的 PATH 问题。
- `CODEX_EXTERNAL_VM_WORKSPACE` 表示容器工作区在 VM 上的挂载根路径（默认 `/srv/nfs/my-agent/workspace`）。

## 功能开关（codex / clawhub / OpenViking）

部署时（`deploy-all.ps1` 勾选 tool-runtime 后的子选项）可分别启用/停用 codex 与 clawhub；OpenViking 由是否勾选 `openviking-server` 决定：

| 环境变量 | 默认 | 说明 |
|----------|------|------|
| `ENABLE_CODEX` | `true` | 为 `false` 时调用 `codex` 工具会返回「Codex 未启用」提示 |
| `ENABLE_CLAWHUB` | `true` | 为 `false` 时 `clawhub-search` / `clawhub-install` / `clawhub-list` / `skill-delete` 会返回「ClawHub 未启用」提示 |
| `ENABLE_OPENVIKING` | `true` | 为 `false` 时 `skill-list` / `skill-abstract` / `skill-overview` / `skill-manual` / `add-skill-to-viking` 会返回「OpenViking 未启用」提示 |

已安装技能的执行（`run_skill`）不依赖 clawhub / OpenViking，不受上述开关影响。

## 构建镜像

```bash
cd tool-runtime-service
docker build --no-cache --progress=plain -t agent/tool-runtime-service:v45 .
```

版本号需与 `deploy/tool-runtime-apply.sh` 中的 `TOOL_RUNTIME_IMAGE`（默认 `agent/tool-runtime-service:v45`）保持一致。

## 导入 kind 节点

如果是本地 kind / Docker Desktop，并且 YAML 使用 `imagePullPolicy: Never`：

```bash
docker save agent/tool-runtime-service:v45 | docker exec -i desktop-control-plane ctr -n k8s.io images import -
docker save agent/tool-runtime-service:v45 | docker exec -i desktop-worker ctr -n k8s.io images import -
```

## 部署

通过 `deploy/tool-runtime-apply.sh` 部署（脚本内嵌 Deployment + Service YAML，自动生成 SSH 密钥、配置外部 VM 执行环境并 apply）：

```bash
bash deploy/tool-runtime-apply.sh
```

常用环境变量：`TOOL_RUNTIME_IMAGE`、`CLAW_EXTERNAL_VM_HOST`、`CLAW_EXTERNAL_VM_USER`、`CLAW_EXTERNAL_VM_PORT`、`CLAW_EXTERNAL_VM_SKILL_ROOT_DIR`、`OPENVIKING_SERVER_URL` 等，见脚本头部默认值。

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

# tool-runtime-service

这是 My_Agent MSA 的 tool-runtime-service 拆分版骨架服务。

## 端口

- gRPC: `5303`
- Kubernetes Service: `tool-runtime-service.agent.svc.cluster.local:5303`
- 同 namespace 内短域名: `tool-runtime-service:5303`

## 当前支持的工具

通过 `ExecuteToolRequest.tool_name` 调用（短横线命名，OpenClaw/ClawHub 风格），完整列表见服务内 `help`：

### 文件 / 工作区

- `echo`：返回文本
- `list-workspace`：列出工作空间所有文件
- `dir-list|目录|通配符`：列出目录（`recursive=false` 只看当前层）
- `file-read|文件路径`：读取文件（上限 `MAX_READ_BYTES`，默认 1MB）
- `file-write|文件路径|内容`：写入（覆盖）
- `file-append|文件路径|内容`：追加
- `file-upload|路径|base64`：上传（最大 48MB）
- `file-copy|源|目标` / `file-move|源|目标`：复制/移动；Windows 路径自动转 `windows-*` 逻辑
- `windows-file-copy` / `windows-file-move`：与 Windows 宿主机（/mnt）复制/移动
- `file-tail|文件路径|行数`：读末尾 N 行（默认 50）
- `file-search|关键词|路径|条数`：内容搜索（支持正则）
- `delete-file|路径`：删除文件或空目录
- `unzip|压缩包|目标目录`：解压
- `download|url|目标路径`：下载到工作区

### 网络 / 信息获取

- `fetch|url|method|data`：HTTP 请求 + 智能处理（见下方「fetch 行为矩阵」）
- `web-search|关键词|条数`：SearXNG 元搜索（默认 10 条，最多 50）
- `port-expose|端口`：声明对外开放端口（5800-5899），返回访问链接

### 图片 / 消息

- `get-image-url-from-local|路径`：本地图片 → URL
- `send-image-by-url|url`：立即推送图片给用户
- `send-message|text` / `notify`：执行过程中立即推送文本

### PROCESS 长期事件记录（按 user_id + agent_id 定位存储）

- `process-write|index|title|content`（index=-1 追加，1..N 覆写）
- `process-remove|index` / `process-init`

### 世界书（World Info）长期设定（scope 默认当前 agent，无全局）

- `worldinfo-write|关键词1,关键词2|内容|优先级|scope|constant|regex`（scope 省略=当前 agent；群组用 `group:群组id`；同 scope 同关键词覆写）
- `worldinfo-remove|条目id` / `worldinfo-list`（模型自查已有哪些设定）

### 代码 / 系统

- `shell` / `run-shell` / `command`：执行命令（默认关闭，`ENABLE_SHELL_TOOLS=true` 启用）
- `codex|工作目录|需求`：通过 SSH 在外部 VM 执行 codex 代码生成（见下方说明）

### 技能（ClawHub / OpenViking）

- `clawhub-search|关键词` / `clawhub-install|技能名` / `clawhub-list`
- `skill-list` / `skill-list-simple` / `skill-delete|技能名`
- `skill-abstract` / `skill-overview` / `skill-manual|技能名`
- `add-skill-to-viking|技能名`
- 未命中的工具名自动按已安装技能执行（`skill_runtime`）

## fetch 行为矩阵（v50+）

`fetch|url|method|data`，GET 时第 4 参 `data` 是搜索/定位依据（多词空格分隔），行为按清洗后文本长度 L 路由：

| 情况 | 行为 |
|---|---|
| L ≤ 1500 | 原始内容 |
| 1500 < L ≤ 6000 | `data` 有 → reader 提取「从中提取出与下列相关的原文内容：{data}」；`data` 空 → 完整文本 |
| L > 6000 且 `data` 空 | 页面大纲（带【N】序号章节 + 下一步提示） |
| L > 6000 且 `data`=序号/标题词 | 返回该章节整块内容 |
| L > 6000 且 `data`=具体问题 | 语义召回（向量相似度 → LLM 分块提取 → 关键词 降级链） |

- 语义召回使用 model-proxy 的 Embedding RPC（`model_list.json` 中 `default-embedding` 对应的嵌入模型）
- reader 提取 / LLM 提取使用 model-proxy ChatCompletion（`default-reader`）
- 请求带浏览器 UA，规避常见反爬

## 内置环境

- **Playwright + Chromium 无头浏览器**（v51 起）：镜像内置完整 Chromium，可用 `from playwright.sync_api import sync_playwright` 直接启动（容器内为 root，需传 `--no-sandbox`）
- **model_proxy_client**：`app/model_proxy_client.py`，封装 model-proxy 的 ChatCompletion / Embedding gRPC（供 fetch 语义召回与提取）
- **SearXNG sidecar**：同一 Pod 内 8080 端口，供 `web-search` 使用

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
docker build --no-cache --progress=plain -t agent/tool-runtime-service:v51 .
```

版本号需与 `deploy/tool-runtime-apply.sh` 中的 `TOOL_RUNTIME_IMAGE`（默认 `agent/tool-runtime-service:v51`）保持一致。

## 导入 kind 节点

如果是本地 kind / Docker Desktop，并且 YAML 使用 `imagePullPolicy: Never`：

```bash
docker save agent/tool-runtime-service:v51 | docker exec -i desktop-control-plane ctr -n k8s.io images import -
docker save agent/tool-runtime-service:v51 | docker exec -i desktop-worker ctr -n k8s.io images import -
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

# 工作进度 — 2026-07-20

## 本次完成：OpenViking 0.3.6 → 0.4.5 升级

### 升级内容
| 组件 | 旧版本 | 新版本 |
|---|---|---|
| OpenViking 服务端 | `ghcr.io/volcengine/openviking:v0.3.6` | `v0.4.5` |
| Context Service 客户端 | `openviking==0.3.6` | `0.4.10` |
| Tool-Runtime 客户端 | `openviking==0.3.6` | `0.4.10` |

### 遇到的问题及修复
1. **`url` 参数未传入** — 0.4.10 `AsyncHTTPClient.__init__` 改为 `*args/**kwargs`，`viking_store.py` 和 `skill_runtime.py` 的 `_client_kwargs` 通过 `inspect.signature` 检测不到 `url` 参数。修复：加 `else` fallback 始终传 `url`
2. **Root API key 被拒** — v0.4.5 服务端禁止 root key 访问数据 API。修复：创建 `my-agent` 账户 + `agent-service` 用户 API key
3. **依赖冲突** — tool-runtime 的 `requests==2.32.5` 与 `openviking==0.4.10` 要求的 `requests>=2.33.0` 冲突。修复：改为 `requests>=2.33.0`

### Skill 方法实现
4 个空桩方法已对接原生 OpenViking API：
- `add_skill_document` → `client.add_skill()`
- `list_skill_docs` → `client.list_skills()`
- `read_skill_doc` → `client.get_skill()`
- `search_skill_docs` → `client.find_skills()`

### API Key 存储
- 不再硬编码到源码/YAML
- 存储于 `/srv/nfs/my-agent/config/openviking/api_key`
- `config.py` 新增 `_read_secret_file()`：env 值若是文件路径 → 读文件内容；否则当普通值

### 镜像版本
| 镜像 | 版本 |
|---|---|
| `agent/openviking-context-service` | v19 |
| `agent/tool-runtime-service` | v22 |

## 待部署

Windows 终端执行：
```powershell
cd E:\github\My_Agent_MSA

# 构建
cd services\openviking-context-service
docker build -t agent/openviking-context-service:v19 .
cd ..\tool-runtime-service
docker build -t agent/tool-runtime-service:v22 .

# 部署
kubectl apply -f ..\..\deploy\services\openviking-context-service.yaml
kubectl apply -f ..\..\deploy\services\openviking-server.yaml
kubectl -n agent rollout restart deploy/openviking-context-service
kubectl -n agent set image deploy/tool-runtime-service tool-runtime-service=agent/tool-runtime-service:v22
kubectl -n agent set env deploy/tool-runtime-service OPENVIKING_API_KEY="/app/system_prompts/openviking/api_key"
```

## 注意事项
- 旧 root key 存储的 skill 数据在新账号下不可见，需重新导入
- Skill 文件路径：`/srv/nfs/my-agent/workspace/skill/`（对应 `\\wsl.localhost\Ubuntu-24.04\srv\nfs\my-agent\workspace\skill`）
- 当前 WSL 机器 Docker/kubectl 均 SIGBUS 崩溃，需重启设备

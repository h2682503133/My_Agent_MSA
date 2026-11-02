# My_Agent_MSA

> 基于 **gRPC + Python** 的多智能体平台：多渠道接入、任务调度、Agent 编排、记忆压缩、工具执行、实时事件流。
> 可跑在 **k8s / Docker Compose / 裸机 Python** 三种环境（含"只有 Windows + Python"的最小档位）。
## 基本定位

个人生活助手和聊天机器人，可以做些简单操作，复杂的coding还是请自行使用现有工具

## 核心设计理念

> **不要让智能体知道不该知道的东西，高度链路透明，高度自定义。**

这三句话是硬约束，不是口号，落地方式如下：

| 理念 | 具体做法 |
|---|---|
| **不让智能体知道不该知道的东西** | 用不到的信息不注入模型：工具目录**按环境能力 + 用户权限档位双重裁剪**后才进提示词——没装的能力不出现（`capability.json`），档位不够的工具连名字都不给模型看（未知工具默认按最高档，fail-closed）；提示词里只写"你能用什么"，不把权限规则本身铺给模型 |
| **高度链路透明** | 智能体收到的**模型请求**、发起的**工具调用**（含参数与结果）、任务在服务间的流转，都能在 **dashboard 直接看到**：一次请求从用户消息 → 模型请求 → 工具调用 → 回复推送，全程可追溯，不靠翻容器日志去猜 |
| **高度自定义** | 提示词/世界书/智能体/模型清单/工具档位/表情绑定/记忆参数全部是**可在 dashboard 改的文件**，不是代码常量；部署按**功能**选而不是按服务选；同一功能有多种实现方式（本地/外部/容器）可换 |

## 这是什么

把「用户消息 → 智能体思考 → 工具/记忆/模型调用 → 回复推送」拆成一组可独立部署的服务：

```
浏览器(chat)  ── SSE ──┐
QQ(Satori/OneBot) ─────┤
定时任务 ──────────────┴─→ gateway / qq adapter ─→ task-scheduler（队列 + 事件总线）
                                                        ↓ gRPC
                                              agent-orchestrator（编排核心）
                                     ├── model-proxy（模型路由 + 生图/embedding）
                                     ├── openviking-context 或 本地记忆（压缩/召回）
                                     ├── tool-runtime（工具 / 技能 / 浏览器 / 代码生成）
                                     └── timer-task（定时）
                                              ↑
                                    dashboard（5700，管理面：配置/权限/日志/状态）
```

14 个服务，后端全部是 Python；网页前端是静态页（k8s/compose 用 nginx，单体档由 Python 内置服务替代），管理面板是 .NET WPF。
**无数据库、无 Redis**：配置与数据都是文件，落在数据目录 / 共享存储里，删目录即干净。

## 三种部署路线（按你的环境选）

| 档位 | 你的环境 | 路线 | 入口 |
|---|---|---|---|
| **T2 / T5** | Windows + Docker Desktop k8s / 任意 k8s | k8s 清单 + 面板「一键部署」 | `admin-panel/publish/MyAgentAdminPanel.exe` 或 `deploy-all.ps1` |
| **T1 / T4** | 有 Docker，没有 k8s | Docker Compose（按 profile 开功能） | `cd deploy/compose && docker compose up -d` |
| **T0 / T3** | 只有 Windows/Linux + Python | 单体模式（launcher 起本地子进程） | `deploy/launcher/deps.py --install` → `run.py` |

> 完整步骤、每个选项的含义（记忆 `commit_limit`、`capability.json`、工具档位 L0–L4、浏览器链路、QQ 两种后端…）
> 见 **[部署指南.md](部署指南.md)**；卸载见 **[卸载指南.md](卸载指南.md)**。

**最小可用（T0，无 Docker）**：

```powershell
python -X utf8 deploy/launcher/deps.py --data-dir "$env:LOCALAPPDATA\MyAgent\data" --features core,memory --install
python -X utf8 deploy/launcher/run.py   --data-dir "$env:LOCALAPPDATA\MyAgent\data" --features core,memory
# 网页 http://127.0.0.1:8080/login.html   管理面 http://127.0.0.1:5700
```

启动后**一定要做**：dashboard →「配置管理」→ `model-proxy/config/model_list.json` 换成真实 `api_url/api_key/model`（改完不用重启）。

## 核心能力

- **多渠道**：Web（SSE 实时流）、QQ Satori（`qq-llbot-service`）、QQ OneBot 11（`qq-onebot-service`，连 NapCat/LLOneBot/go-cqhttp）、QQ 官方（`qq-official-service`）
- **多智能体**：`main` / `tool` / `reader` / `talker` / `writer` 角色编排，支持智能体互相调用、扇出与合流
- **记忆**：OpenViking 语义检索；**未部署时自动降级**为本地两级记忆（窗口滚动 + 按 `commit_limit` 用 reader 模型压缩成长期摘要，压缩失败保留原文不丢数据）
- **工具与技能**：文件/网络/搜索/生图/浏览器等内置工具、clawhub 技能、codex 代码生成、dsh，全部**按能力与档位裁剪**
- **权限模型**：TOOL.json 驱动的 L0–L4 五档 + 用户黑名单（`tool-blacklist` 自助加入、管理员移出），全部在 dashboard「工具权限」页
- **可观测**：dashboard 提供 Pod/服务状态、日志流、运行环境与降级原因、会话与用户列表
- **按功能部署**：`deploy/features.json` 是唯一事实来源——功能⇒服务、功能⇒需要下载什么、哪些实现方式只在某些环境可用

## 设计边界（刻意不做的事）

- **不写注册表、不做开机自启、不装系统服务**：所有状态都在数据目录里，删目录即干净
- **dashboard 只是管理面**：Windows 侧操作（浏览器托管、扫码、端口转发）统一在 `admin-panel`；集群侧状态展示才在 dashboard
- **不改模板**：仓库 `config/` 是模板，运行时读数据目录里的副本，部署只做"复制 + 生成"
- **不静默降级**：能力缺失会写进 `capability.json.degraded` 并在 dashboard 显示；工具档位缺失按 fail-closed 处理

## 当前状态

| 路线 / 能力 | 状态 |
|---|---|
| k8s 路线（T2 / T5） | ✅ 完整可用 |
| Docker Compose 路线（T1 / T4） | 🧪 清单与校验就绪，待实机验收 |
| 单体路线（T0 / T3，无 Docker） | ✅ Windows 11 干净虚拟机实测通过：装 Python → `deps.py --install` → `run.py` 起 7 个服务 → `8080` 可访问（core+memory）；Linux 待验收 |
| 结构化链路视图（按 `task_id` 串起请求/工具/回复） | 🚧 规划中；当前可从 dashboard「日志」页看到编排器的请求与调用记录 |
| 单体档图床 | ✅ 由 `deploy/launcher/assets.py` 提供 `/assets/<file>`（5102），启动器自动拉起 |
| `external-exec` 本地执行 | 🚧 目前 codex 走 SSH 到外部 VM |

## 快速开始（k8s 路线）

1. 装 Docker Desktop（启用 Kubernetes）+ kubectl；跨进程共享配置需 RWX 存储（NFS / NAS / 单节点 hostPath）
2. 打开 `admin-panel/publish/MyAgentAdminPanel.exe` →「🚀 一键部署」→ 勾功能 → 预览计划 → 构建并部署
3. dashboard（`http://<宿主IP>:5700`）登录 →「配置管理」填 `model_list.json` → 发消息验证

局域网访问：`bash deploy/lan-expose.sh`（NodePort + socat，`--down` 拆除）。

## 仓库结构

```
services/            14 个服务（每个都有独立 README：端口、环境变量、构建）
  agent-orchestrator-service   编排核心：提示词、工具注入、权限闸门、记忆
  task-scheduler-service       队列 + 事件总线（订阅优先级/ack/缓冲）
  gateway-backend-service      FastAPI + SSE 网关
  dashboard-service            管理面（5700）
  tool-runtime-service         工具执行（含浏览器、技能、代码生成）
  model-proxy-service          模型路由（chat/embedding/image）
  qq-*.service                 QQ 三种后端
  ...
deploy/
  features.json        功能清单（部署唯一事实来源）
  services/*.yaml      k8s 清单
  compose/             docker-compose + .env 模板
  launcher/            单体模式（plan/run/deps/web）
  features/probes      probe_env.py（环境探测 → capability.json）
admin-panel/           .NET 8 WPF 管理面板（Windows 侧操作）
config/                最简配置模板（复制到数据目录后使用）
部署指南.md / 卸载指南.md / 项目介绍.md
```

## 自检（离线，不需要集群）

```powershell
# 部署骨架
python -X utf8 deploy/launcher/launcher_test.py        # 功能→服务、端口、环境变量
python -X utf8 deploy/launcher/deps_test.py            # 依赖锁与冲突报告
python -X utf8 deploy/launcher/config_init_test.py     # 配置模板复制（不覆盖）
python -X utf8 deploy/launcher/web_test.py             # 单体前端静态 + /api 反代（真进程回环）
python -X utf8 deploy/model_config_test.py             # 模型清单生成
python -X utf8 deploy/compose/compose_lint.py          # compose 清单校验
python -X utf8 deploy/gen_protos.py --check            # gRPC stubs 是否齐全

# 编排核心
python -X utf8 services/agent-orchestrator-service/tools/tool_permissions_test.py
python -X utf8 services/agent-orchestrator-service/tools/tool_injection_filter_test.py
python -X utf8 services/agent-orchestrator-service/tools/capabilities_test.py
python -X utf8 services/agent-orchestrator-service/tools/local_context_test.py
python -X utf8 services/qq-onebot-service/tools/onebot_protocol_test.py
```

## 文档导航

| 文档 | 内容 |
|---|---|
| [部署指南.md](部署指南.md) | 三种环境怎么装、每个选项什么意思、部署后自检清单 |
| [卸载指南.md](卸载指南.md) | 三条路线的卸载顺序、安全边界、备份与验证 |
| [项目介绍.md](项目介绍.md) | 架构细节、服务端口、配置清单、QQ 渠道、NFS |
| [deploy/README.md](deploy/README.md) | 部署脚本说明（NFS / PV / 服务 YAML） |
| [admin-panel/README.md](admin-panel/README.md) | Windows 管理面板 |
| `services/*/README.md` | 各服务端口、环境变量、构建说明 |

## 技术栈

- **运行时**：Python 3.11；k8s（Docker Desktop / kind）或 Docker Compose 或裸机
- **通信**：gRPC（内部）+ FastAPI/SSE（对外）+ WebSocket（QQ 渠道）
- **模型**：OpenAI 兼容 API / Ollama（本地）；embedding 兼容 `/api/embed` 与老版 `/api/embeddings`
- **记忆**：OpenViking（可选）或本地文件记忆
- **前端**：Nginx 静态页 + 原生 JS（单体档由 `deploy/launcher/web.py` 替代）
- **管理**：.NET 8 WPF（admin-panel）+ Web dashboard

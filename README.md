# My_Agent_MSA

> 基于 **Kubernetes + gRPC + Python** 的多智能体微服务架构：多渠道接入、任务调度、Agent 编排、RAG 长期记忆、工具执行、实时事件流，开箱即用的一键部署。

## 这是什么

My_Agent_MSA 是一套完整的 AI Agent 平台，把「用户消息 → 智能体思考 → 工具/记忆/模型调用 → 回复推送」拆成一组可独立部署的微服务：

- **多渠道**：Web 聊天（SSE 实时流）与 QQ（Satori 协议）共用同一条任务链路
- **多智能体**：`main` / `tool` / `reader` 三角色编排，支持自定义人设、智能体互相调用
- **长期记忆**：OpenViking 语义检索（RAG），未部署时自动降级为本地最近 4 回合上下文
- **工具与技能**：文件/网络/搜索/图片等内置工具，clawhub 技能与 codex 代码生成（可按需开关）
- **定时任务**：到期自动执行 Agent 链路或直接推送消息
- **一键部署**：选择「必要服务」即可跑通 Web 对话闭环，其余功能按需勾选，版本号、配置同步、OpenViking 初始化全部自动化

## 架构一图流

```
浏览器 / QQ
   ↓
frontend (nginx) ── gateway-backend (SSE) ── task-scheduler ── agent-orchestrator
                                              ↓ 调用           ├── model-proxy（模型）
                                                              ├── openviking-context（记忆）
                                                              ├── tool-runtime（工具/技能）
                                                              └── timer-task（定时）
```

## 快速开始（新电脑约 30 分钟）

1. **安装**：Docker Desktop（启用 Kubernetes）+ kubectl + Git；注册 Moonshot 获取模型 API Key
2. **配置**：`config/model-proxy/config/model_list.json` 填 API Key（或改用环境变量）；如用 OpenViking 记忆再填 `config/openviking/ov.conf` 的 `vlm.api_key`
3. **部署**：项目根目录运行 `powershell deploy-all.ps1`，默认已勾选「必要服务」，Enter 确认即完成构建与部署，浏览器访问 `http://localhost:8080/login.html`

> 详细部署步骤、服务清单、配置说明见 **[项目介绍.md](项目介绍.md)**

## 文档导航

| 文档 | 内容 |
|------|------|
| [项目介绍.md](项目介绍.md) | 架构、服务端口、核心功能、部署流程、配置清单、QQ 渠道、NFS |
| [常见问题处理.md](常见问题处理.md) | 常见问题排查 |
| [上下文管理.md](上下文管理.md) | 上下文与记忆管理 |
| [deploy/README.md](deploy/README.md) | 部署脚本说明（NFS / PV / 服务 YAML） |
| [admin-panel/README.md](admin-panel/README.md) | Windows 管理面板 |
| `services/*/README.md` | 各微服务说明（端口、环境变量、构建） |

## 技术栈

- **运行时**：Kubernetes（Docker Desktop / kind）、Docker、WSL2
- **后端**：Python 3.11、FastAPI、gRPC、SSE
- **模型**：OpenAI 兼容 API（Moonshot 等）/ 本地 Ollama
- **记忆**：OpenViking（语义检索）
- **前端**：Nginx 静态页 + 原生 JS
- **管理**：.NET 8 WPF 桌面面板（admin-panel）

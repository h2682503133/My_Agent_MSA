# dashboard-service

My_Agent MSA 的管理控制面板：FastAPI 后端 + 静态 Web UI，负责配置管理、集群状态查看、日志、System Prompt 管理、Session 管理、长期记忆查询等日常运维功能。

## 端口与命名

```text
namespace: agent
image: agent/dashboard-service:v30      （版本号以 deploy/services/dashboard-service.yaml 的 image 字段为准）
service: dashboard-service
port: 5700 (HTTP)
访问: kubectl -n agent port-forward svc/dashboard-service 5700:5700 → http://localhost:5700
```

## 功能

| 模块 | API | 说明 |
|------|-----|------|
| 认证 | `POST /api/auth/login` / `POST /api/auth/change-password` | 面板登录与改密，密码存于 config 卷 `dashboard_password.json`，默认 `123456` |
| 配置管理 | `GET/PUT /api/config/{path}`、`GET /api/config` | 读写 `CONFIG_ROOT` 下的配置文件（排除 system_prompt 与 process 目录） |
| Pod/Service 状态 | `GET /api/pods`、`GET /api/services` | 调用 kubectl 查询集群状态 |
| 重启 | `POST /api/restart?service=xxx` | `kubectl rollout restart deployment` |
| 日志 | `GET /api/logs?pod=xxx`、`GET /api/logs/stream?pod=xxx` | 单次拉取 / SSE 流式日志 |
| 部署脚本 | `GET /api/deploy/script?services=a,b` | 生成一键部署 shell 脚本 |
| System Prompt | `GET/POST/PUT/DELETE /api/system_prompt/**` | 管理每个智能体的 System Prompt 文件；列表里会标出 `TOOL.md` 是「选项式 / 原文式」、`TOOL.json` 的段/工具数与 JSON 错误 |
| 工具目录 | `GET /api/tool/catalog`、`GET /api/tool/options?agent=`、`POST /api/tool/generate` | 解析 `system_prompt/TOOL.json`、反解某智能体 `TOOL.md` 的选项块、按勾选生成 `TOOL.md` + 注入预览 |
| QQ 设置 | `POST/GET /api/qrcode`、`GET /api/qrcode/info`；`GET /api/qq/channels`、`POST /api/qq/bind`、`POST /api/qq/unbind` | 两个子页：LLBot（登录二维码，沿用原二维码接口）、官方 Bot（`qqofficial` 渠道的 openid ↔ 规范 user_id 绑定，转发给 user-service） |
| Session 管理 | `GET /api/session/**` | 按 openviking 用户列出/查看/删除 session 及 messages.jsonl |
| 长期记忆 | `GET /api/memory/search?user=&query=` | 调 OpenViking `/api/v1/search/find` 做用户级语义搜索 |
| 杂项设置 | `GET/PUT /api/misc/settings` | 读写 orchestrator 的 system_settings.json（图片接收、shell 限制等开关） |

## System Prompt 页面的工具目录面板

工具清单统一维护在 `orchestrator/system_prompt/TOOL.json`，各智能体 `TOOL.md` 只写注入选项。页面上：

- 点「全局文件 → TOOL.json」：结构化展示目录（每段默认注入/默认关闭、工具与分组标签）+ 各智能体 `TOOL.md` 状态一览，可一键「格式化 JSON 到编辑器」。
- 点某智能体的 `TOOL.md`：出现**勾选生成器**——按段/工具（支持分组整组勾选、名称过滤）勾选，填「正文补充」，点「只预览 / 生成到编辑器 / 生成并保存」，即写出该智能体的 `TOOL.md`（选项块 + 正文）。
- 老式手写 `TOOL.md`（没有 `tool-inject` 选项块）会被识别为「原文式」：原内容自动放进「正文补充」，勾选生成后即转换为选项式；生成前可预览 orchestrator 实际注入的文本。
- 没有 `TOOL.md` 但 `agent_list.json` 已声明该文件的智能体，列表里会出现「＋ 生成 TOOL.md」入口。

勾选 → 选项块的规则实现在 `app/tool_catalog.py`，与 orchestrator 的 `app/context_builder.py` 保持一致（两个容器无法共用代码，改动时需同步）。

## 静态资源结构（static/）

前端按「上方标签页」拆分，方便单独维护：

```text
static/
├── index.html                页面骨架（约 80 行）：head / 登录层 / header+nav / <main> 里的 @include 占位 / script 引入
├── assets/
│   ├── css/dashboard.css     全部样式（变量 / 布局 / 通用组件 / 标签页专用）
│   ├── tabs/<tab>.html       每个标签页的标记分片：pods config logs sprompt worldinfo process session qq misc
│   └── js/
│       ├── core.js           公共：登录·改密、Tab 切换、Toast、esc / escapeHtml
│       ├── pods|config|logs|sprompt|tool_panel|worldinfo|process|session|qrcode|qq|misc.js
│       └── init.js           启动（首次刷新 + 15s 定时器），必须最后加载
└── qrcode.png / qrcode_time.txt   LLBot 二维码（由 POST /api/qrcode 写入）
```

- `/assets` 由 FastAPI `StaticFiles` 挂载；`GET /` 读取 `index.html`，并把 `<!-- @include: tabs/xxx.html -->` 占位替换为对应分片内容（手写 include，不引模板引擎依赖；路径相对 `static/assets/`，越界路径拒绝）。分片缺失时页面里会留 `<!-- @include 分片不存在: xxx -->`，方便定位。
- 改分片 / 样式 / JS 后刷新页面即可生效，**不需要重启服务**；但静态文件在镜像里，改完仍需重建镜像才带到集群。
- 新增一个标签页：① `assets/tabs/<tab>.html` 写标记；② `index.html` 的 `<nav>` 加按钮、`<main>` 加 `@include`、末尾加 `<script src="/assets/js/<tab>.js">`；③ 新建 `assets/js/<tab>.js`；④ 在 `core.js` 的 Tab 切换处挂上该页签需要调用的刷新函数。

## 环境变量

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `CONFIG_ROOT` | `/app/config` | 配置根目录（挂载 my-agent-config-pvc） |
| `NAMESPACE` | `agent` | kubectl 查询的命名空间 |
| `SESSION_ROOT` | `/app/session-data/workspace/viking/my-agent/user` | openviking 用户 session 根目录（挂载 my-agent-openviking-pvc） |
| `OPENVIKING_SERVER_URL` | `http://openviking.agent.svc.cluster.local:1933` | OpenViking 语义搜索服务地址 |
| `USER_SERVICE_URL` | `http://user-service.agent.svc.cluster.local:5204` | user-service HTTP 地址（取 per-user OpenViking key） |
| `OPENVIKING_API_KEY` | `/app/config/openviking/api_key` | OpenViking key 文件路径（用户无独立 key 时回退） |

## 依赖

- 镜像内安装 `kubectl`，Pod 通过 `dashboard-sa` ServiceAccount 访问集群。
- RBAC（见 `deploy/services/dashboard-service.yaml`）：`pods`、`pods/log`、`services`、`endpoints`、`namespaces` 只读 + `deployments` 可 patch（重启用）。
- 依赖两个 PVC：`my-agent-config-pvc`（/app/config）、`my-agent-openviking-pvc`（/app/session-data）。

## 构建镜像

版本号只维护在 `deploy/services/dashboard-service.yaml` 的 `image` 字段（`deploy-all.ps1` 会从那里解析 tag），改完记得同步这里：

```bash
docker build -t agent/dashboard-service:v30 services/dashboard-service
```

## 部署

```bash
kubectl apply -f deploy/services/dashboard-service.yaml     # 或 kubectl -n agent rollout restart deployment/dashboard-service
```

## 检查

```bash
kubectl get pods -n agent -l app=dashboard-service
kubectl logs -n agent deployment/dashboard-service
kubectl -n agent port-forward svc/dashboard-service 5700:5700
curl http://localhost:5700/api/pods
```

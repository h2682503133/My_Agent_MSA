# Compose 部署（无 k8s，T1 / T4）

给"装了 Docker、没装 k8s"的用户用的第二条路线。**核心对话 + 面板零配置**，其余功能按 profile 打开。

## 与其他路线的关系

| 路线 | 目录 | 适用 |
|---|---|---|
| T0/T3 单体 | （S4 待做） | 只有源码 + Python |
| **T1/T4 compose** | `deploy/compose/` | 有 Docker（Windows/Linux） |
| T2/T5 k8s | `deploy/services/` | 有集群（现状） |

功能定义统一来自 [../features.json](../features.json)，profile 与功能的对应写在 compose 文件头注释里。

## 用法

```bash
cd deploy/compose
cp .env.example .env          # 按需改端口/SearXNG/QQ 地址
mkdir -p data/config data/workspace data/images data/users

# 1) 把配置放进来（首次可用仓库里的模板）
cp -r ../../config/* data/config/

# 2) 核心（模型代理 + 用户 + 调度 + 编排 + 网关 + 前端 + 面板 + 图床）
docker compose up -d

# 3) 按需叠加
docker compose --profile tools  up -d     # 工具调用（文件/生图/表情绑定/fetch…）
docker compose --profile memory up -d     # 长期记忆（openviking + embedding）
docker compose --profile search up -d     # 联网搜索（SearXNG 容器；不启则用内置 Bing CN 回落）
docker compose --profile qq     up -d     # QQ（Satori：连宿主/别人的 llbot）
docker compose --profile qq-official up -d
docker compose --profile timer  up -d     # 定时任务

# 看状态 / 日志
docker compose ps
docker compose logs -f agent-orchestrator-service
```

端口默认：网页 `8080`、网关 API `5210`、面板 `5700`、图床 `5102`（都在 `.env` 里可改）。

## 数据与配置

全部落在 `deploy/compose/data/`（bind mount，可直接备份/打包带走）：

```
data/config/orchestrator/{config,system_prompt}   ← 编排配置 + 提示词
data/config/model-proxy/config                    ← 模型清单
data/config/openviking/                           ← OpenViking 配置与 key
data/config/qq-llbot/ · qq-official/              ← QQ 适配器配置
data/workspace/                                   ← 用户工作区（编排与工具共用）
data/images/                                      ← 图床目录
data/session/ · data/users/ · data/timer-tasks/   ← 会话/用户/定时任务
```

`capability.json` 放 `data/config/capability.json`（编排与面板都读它；可用 `deploy/features.json` 的探测脚本生成）。

## 与 k8s 路线的差异（已知限制）

| 项 | compose | k8s 现状 |
|---|---|---|
| 服务发现 | 服务名 DNS（已把 `*.svc.cluster.local` 全部换掉） | 集群 DNS |
| 存储 | bind mount（Windows 上走 NTFS，性能一般） | NFS PVC |
| 宿主浏览器托管 | ❌ 需要宿主侧面板/MCP 中继 | ✅ admin-panel |
| 局域网保留端口 | 只用 `ports:` 映射 | NodePort + socat |
| 多节点 | ❌ | �� |
| 定时任务 | `profile timer` | 独立 Deployment |

> 浏览器、宿主文件、外部 VM 执行（dsh/codex）在 compose 里只能"**连已有的端点**"（填 `BROWSER_MCP_URL` / SSH 信息），不做托管——托管能力仍归 admin-panel。

## 自检（不启动容器）

```bash
python -X utf8 deploy/compose/compose_lint.py
```

检查项：YAML 结构、build 上下文存在、core 服务齐全、profile 与 `features.json` 对应、环境变量里的服务名都已定义、卷都在 `./data` 下、无集群内 DNS 残留。

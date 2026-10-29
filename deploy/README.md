# 部署指南

## 部署流程（4 步）

在 WSL 中按顺序执行：

### 1. 初始化 NFS
```bash
sudo bash deploy/setup-nfs.sh
```

### 2. 同步配置到 NFS
```bash
bash deploy/sync-config.sh
```

### 3. 创建 PV/PVC
```bash
NFS_SERVER=172.29.219.49 NFS_ROOT=/srv/nfs/my-agent bash deploy/apply-pv.sh
```

### 4. 部署服务
```bash
kubectl apply -f deploy/services/
```

## 服务端口

| 服务 | 端口 | 类型 | 说明 |
|---|---|---|---|
| gateway-backend-service | 5210 | ClusterIP（+ NodePort 30521） | 网关 API；同时内含管理面板反代 |
| image-assets-service | **5102** | port-forward | 图床（nginx，直连宿主机） |
| frontend-service | 80 | ClusterIP | 网页前端（经 Istio ingress） |
| qq-satori-adapter | 5600 | ClusterIP + Endpoints | QQ Satori 适配器（旧的外部 adapter 壳） |
| llbot | 5600 / 3080 | ClusterIP | 集群内 LLBot（有头/无头），WebUI 3080 |
| dashboard-service | 5700 | ClusterIP（+ NodePort 30570） | 管理面板 |

### 局域网访问（保原端口）

`kubectl port-forward` 是用户态进程，入站要过 Windows 防火墙（Domain profile 常拦）；
而 Docker 发布的端口走 Docker 自己的放行规则（80 端口能直连就是这个原因）。
所以需要「按原端口给局域网访问」的界面，用 `lan-expose`：

```bash
bash deploy/lan-expose.sh            # 建 NodePort + 起 socat 容器（幂等）
bash deploy/lan-expose.sh --check    # 探测端口
bash deploy/lan-expose.sh --status   # 看 Service / 容器
bash deploy/lan-expose.sh --down     # 拆除
```

对照关系（宿主端口 = 服务端口，NodePort 固定 30xxx）：

```
5210 网关 API      → 30521
5700 管理面板      → 30570
3080 LLBot WebUI  → 30080
```

可覆盖：`NODE_IP`（节点 IP，默认按 control-plane 自动探测）、`KIND_NETWORK`（默认 `kind`）、
`SOCAT_IMAGE`（默认 `alpine/socat:latest`，国内可给 `hub.rat.dev/alpine/socat:latest`）。

## 目录结构

```
deploy/
├── setup-nfs.sh            # 1. NFS 初始化（WSL 内执行）
├── sync-config.sh          # 2. 同步 config 到 NFS
├── apply-pv.sh             # 3. 渲染并 apply PV/PVC
├── pv-templates/           # PV/PVC 模板
│   └── my-agent-nfs-pv-pvc.yaml.tpl
├── services/               # 4. 所有 K8s 服务 YAML
│   ├── agent-orchestrator-service.yaml
│   ├── task-scheduler-service.yaml
│   ├── timer-task-service.yaml
│   ├── gateway-backend-service.yaml
│   ├── image-assets-service.yaml     # 图床（nginx，port-forward 5102）
│   ├── qq-llbot-service.yaml
│   ├── qq-official-service.yaml      # QQ 官方机器人（WebSocket，无需公网入口）
│   ├── qq-satori-adapter-external.yaml
│   ├── embedding-service.yaml        # 向量计算统一入口（port 5303）
│   ├── model-proxy-service.yaml
│   ├── openviking-context-service.yaml
│   ├── openviking-server.yaml
│   ├── user-service.yaml
│   ├── frontend-service.yaml
│   ├── llbot.yaml                    # 集群内 LLBot（有头 QQ 的核心）
│   ├── llbot-pmhq.yaml               # 有头模式：Xvfb + Linux QQ + pmhq
│   ├── lan-expose.yaml               # 局域网访问（NodePort）
│   └── istio/istio.yaml
├── lan-expose.sh           # 局域网暴露：NodePort + socat 容器（保原端口）
├── llbot-apply.sh          # LLBot 部署：敏感值交互输入（Auth Token / WebUI / satori）
├── llbot-README.md         # 上条脚本的说明与轮换方式
├── lan-forwards.ps1/.cmd   # Windows 侧 kubectl port-forward 守护（备用方案）
└── tool-runtime-apply.sh   # tool-runtime 外部 VM 模式部署
```

## 外部系统

- **dsh（DeepSeek Harness）对接**：在 Windows 宿主机上做，不在集群内 —— 见 [`dsh/对接方式.md`](dsh/对接方式.md)（含通道配置模板 `dsh/onebot.patch.yml` 与绑定脚本 `dsh/dsh-bind.ps1`）。

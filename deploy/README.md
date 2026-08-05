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
| gateway-backend-service | 5210 | ClusterIP | 网关 API |
| image-assets-service | **5102** | port-forward | 图床（nginx，直连宿主机） |
| frontend-service | 80 | ClusterIP | 网页前端（经 Istio ingress） |
| qq-satori-adapter | 5600 | ClusterIP + Endpoints | QQ Satori 适配器 |

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
│   ├── qq-satori-adapter-external.yaml
│   ├── model-proxy-service.yaml
│   ├── openviking-context-service.yaml
│   ├── openviking-server.yaml
│   ├── user-service.yaml
│   ├── frontend-service.yaml
│   └── istio/istio.yaml
└── tool-runtime-apply.sh   # tool-runtime 外部 VM 模式部署
```

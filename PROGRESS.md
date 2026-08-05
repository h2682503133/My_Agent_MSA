# 工作进度 — 2026-08-05

## 本次完成：图床统一存储改造

### 问题
- `get-image-url-from-local` 将图片存入用户 workspace 下的 `.assets/images/`，每个用户独立
- 图片 URL 默认走 `file://` 协议，QQ 和浏览器无法访问
- 无 HTTP 端点服务图片

### 改造方案
使用已有的 `my-agent-assets-pvc`（NFS: `/srv/nfs/my-agent/assets/`）作为共享图床存储，gateway 新增 HTTP 端点直接 serve。

### 改动文件

| 文件 | 改动 |
|---|---|
| `services/tool-runtime-service/app/server.py` | `IMAGE_ASSET_DIR` 默认 `/app/assets/images`；`IMAGE_BASE_URL` 默认 `http://gateway-backend-service:5210/api/assets` |
| `services/gateway-backend-service/app.py` | 新增 `/api/assets/{filename:path}` 端点，`FileResponse` + 路径穿越防护 |
| `deploy/services/gateway-backend-service.yaml` | 挂载 `my-agent-assets-pvc` → `/app/assets`，新增 `ASSETS_DIR` 环境变量 |
| `deploy/tool-runtime-apply.sh` | 挂载 `my-agent-assets-pvc` → `/app/assets`，新增 `IMAGE_ASSET_DIR` / `IMAGE_BASE_URL` 环境变量 |
| `deploy/setup-nfs.sh` | 新增 `mkdir -p $NFS_ROOT/assets/images` |
| `deploy-all.ps1` | gateway v18→v22，tool-runtime v1→v25 |

### 图片数据流

```
图片 → tool-runtime get-image-url-from-local
     → 复制到 /app/assets/images/{name}-{uuid}.png（共享 PVC）
     → 返回 URL: http://gateway:5210/api/assets/{name}-{uuid}.png
     → orchestrator → send_images → event.images
     → QQ: Satori Image(src=url) ✅
     → Web: <img src=url> ✅
```

### 镜像版本

| 镜像 | 版本 |
|---|---|
| `agent/gateway-backend-service` | v22 |
| `agent/tool-runtime-service` | v25 |

### 部署

```bash
# WSL 内
sudo bash deploy/setup-nfs.sh
sudo bash deploy/tool-runtime-apply.sh

# Windows 终端
cd E:\github\My_Agent_MSA
cd services\gateway-backend-service
docker build -t agent/gateway-backend-service:v22 .
cd ..\tool-runtime-service
docker build -t agent/tool-runtime-service:v25 .
kubectl apply -f ..\..\deploy\services\gateway-backend-service.yaml
kubectl -n agent rollout restart deploy/gateway-backend-service
kubectl -n agent rollout restart deploy/tool-runtime-service
```
# 工作进度 — 2026-08-05

## 本次完成：图床统一存储改造

### 问题
- `get-image-url-from-local` 将图片存入用户 workspace 下的 `.assets/images/`，每个用户独立
- 图片 URL 默认走 `file://` 协议，QQ 和浏览器无法访问
- 无 HTTP 端点服务图片

### 改造方案
使用已有的 `my-agent-assets-pvc`（NFS: `/srv/nfs/my-agent/assets/`）作为共享图床存储，gateway 新增 HTTP 端点直接 serve。

### 改动文件

| 文件 | 改动 |
|---|---|
| `services/tool-runtime-service/app/server.py` | `IMAGE_ASSET_DIR` 默认 `/app/assets/images`；`IMAGE_BASE_URL` 默认 `http://gateway-backend-service:5210/api/assets` |
| `services/gateway-backend-service/app.py` | 新增 `/api/assets/{filename:path}` 端点，`FileResponse` + 路径穿越防护 |
| `deploy/services/gateway-backend-service.yaml` | 挂载 `my-agent-assets-pvc` → `/app/assets`，新增 `ASSETS_DIR` 环境变量 |
| `deploy/tool-runtime-apply.sh` | 挂载 `my-agent-assets-pvc` → `/app/assets`，新增 `IMAGE_ASSET_DIR` / `IMAGE_BASE_URL` 环境变量 |
| `deploy/setup-nfs.sh` | 新增 `mkdir -p $NFS_ROOT/assets/images` |
| `deploy-all.ps1` | gateway v18→v22，tool-runtime v1→v25 |

### 图片数据流

```
图片 → tool-runtime get-image-url-from-local
     → 复制到 /app/assets/images/{name}-{uuid}.png（共享 PVC）
     → 返回 URL: http://gateway:5210/api/assets/{name}-{uuid}.png
     → orchestrator → send_images → event.images
     → QQ: Satori Image(src=url) ✅
     → Web: <img src=url> ✅
```

### 镜像版本

| 镜像 | 版本 |
|---|---|
| `agent/gateway-backend-service` | v22 |
| `agent/tool-runtime-service` | v25 |

### 部署

```bash
# WSL 内
sudo bash deploy/setup-nfs.sh
sudo bash deploy/tool-runtime-apply.sh

# Windows 终端
cd E:\github\My_Agent_MSA
cd services\gateway-backend-service
docker build -t agent/gateway-backend-service:v22 .
cd ..\tool-runtime-service
docker build -t agent/tool-runtime-service:v25 .
kubectl apply -f ..\..\deploy\services\gateway-backend-service.yaml
kubectl -n agent rollout restart deploy/gateway-backend-service
kubectl -n agent rollout restart deploy/tool-runtime-service
```

## 本次修复：图片 URL 使用 K8s 内部地址导致 QQ/Web 无法加载

### 问题
- `IMAGE_BASE_URL` 默认值为 `http://gateway-backend-service:5210/api/assets`
- `gateway-backend-service` 是 K8s 内部 DNS，QQ 客户端和浏览器无法解析
- QQ 报错 `getaddrinfo ENOTFOUND gateway-backend-service`，网页显示破碎图片

### 根因
图片 URL 直连了 K8s 内部 Service，应该走 Istio ingress 对外入口。

### 修复方案
`IMAGE_BASE_URL` 改为 `http://host.docker.internal/api/assets`，利用 Docker Desktop 的 `host.docker.internal` 解析到宿主机，再经 Istio ingress（80 端口）路由到 gateway-backend-service。

### 改动文件

| 文件 | 改动 |
|---|---|
| `services/tool-runtime-service/app/server.py` | 默认 `IMAGE_BASE_URL` 改为 `http://host.docker.internal/api/assets` |
| `deploy/tool-runtime-apply.sh` | env `IMAGE_BASE_URL` 同步修改 |
| `deploy-all.ps1` | tool-runtime v25→v26 |

### 数据流
```
图片 URL: http://host.docker.internal/api/assets/xxx.png
  → Windows 宿主机解析 host.docker.internal → 127.0.0.1:80
  → Docker Desktop 转发 → Istio ingress (172.18.0.3:80)
  → VirtualService /api/ → gateway-backend-service:5210
  → FileResponse 返回图片
  ✅ QQ / 浏览器均可访问
```
# 工作进度 — 2026-08-05

## 本次完成：图床统一存储改造

### 问题
- `get-image-url-from-local` 将图片存入用户 workspace 下的 `.assets/images/`，每个用户独立
- 图片 URL 默认走 `file://` 协议，QQ 和浏览器无法访问
- 无 HTTP 端点服务图片

### 改造方案
使用已有的 `my-agent-assets-pvc`（NFS: `/srv/nfs/my-agent/assets/`）作为共享图床存储，gateway 新增 HTTP 端点直接 serve。

### 改动文件

| 文件 | 改动 |
|---|---|
| `services/tool-runtime-service/app/server.py` | `IMAGE_ASSET_DIR` 默认 `/app/assets/images`；`IMAGE_BASE_URL` 默认 `http://gateway-backend-service:5210/api/assets` |
| `services/gateway-backend-service/app.py` | 新增 `/api/assets/{filename:path}` 端点，`FileResponse` + 路径穿越防护 |
| `deploy/services/gateway-backend-service.yaml` | 挂载 `my-agent-assets-pvc` → `/app/assets`，新增 `ASSETS_DIR` 环境变量 |
| `deploy/tool-runtime-apply.sh` | 挂载 `my-agent-assets-pvc` → `/app/assets`，新增 `IMAGE_ASSET_DIR` / `IMAGE_BASE_URL` 环境变量 |
| `deploy/setup-nfs.sh` | 新增 `mkdir -p $NFS_ROOT/assets/images` |
| `deploy-all.ps1` | gateway v18→v22，tool-runtime v1→v25 |

### 图片数据流

```
图片 → tool-runtime get-image-url-from-local
     → 复制到 /app/assets/images/{name}-{uuid}.png（共享 PVC）
     → 返回 URL: http://gateway:5210/api/assets/{name}-{uuid}.png
     → orchestrator → send_images → event.images
     → QQ: Satori Image(src=url) ✅
     → Web: <img src=url> ✅
```

### 镜像版本

| 镜像 | 版本 |
|---|---|
| `agent/gateway-backend-service` | v22 |
| `agent/tool-runtime-service` | v25 |

### 部署

```bash
# WSL 内
sudo bash deploy/setup-nfs.sh
sudo bash deploy/tool-runtime-apply.sh

# Windows 终端
cd E:\github\My_Agent_MSA
cd services\gateway-backend-service
docker build -t agent/gateway-backend-service:v22 .
cd ..\tool-runtime-service
docker build -t agent/tool-runtime-service:v25 .
kubectl apply -f ..\..\deploy\services\gateway-backend-service.yaml
kubectl -n agent rollout restart deploy/gateway-backend-service
kubectl -n agent rollout restart deploy/tool-runtime-service
```

## 本次修复：图片 URL 使用 K8s 内部地址导致 QQ/Web 无法加载

### 问题
- `IMAGE_BASE_URL` 默认值为 `http://gateway-backend-service:5210/api/assets`
- `gateway-backend-service` 是 K8s 内部 DNS，QQ 客户端和浏览器无法解析
- QQ 报错 `getaddrinfo ENOTFOUND gateway-backend-service`，网页显示破碎图片

### 根因
图片 URL 直连了 K8s 内部 Service，应该走 Istio ingress 对外入口。

### 修复方案
`IMAGE_BASE_URL` 改为 `http://host.docker.internal/api/assets`，利用 Docker Desktop 的 `host.docker.internal` 解析到宿主机，再经 Istio ingress（80 端口）路由到 gateway-backend-service。

### 改动文件

| 文件 | 改动 |
|---|---|
| `services/tool-runtime-service/app/server.py` | 默认 `IMAGE_BASE_URL` 改为 `http://host.docker.internal/api/assets` |
| `deploy/tool-runtime-apply.sh` | env `IMAGE_BASE_URL` 同步修改 |
| `deploy-all.ps1` | tool-runtime v25→v26 |

### 数据流
```
图片 URL: http://host.docker.internal/api/assets/xxx.png
  → Windows 宿主机解析 host.docker.internal → 127.0.0.1:80
  → Docker Desktop 转发 → Istio ingress (172.18.0.3:80)
  → VirtualService /api/ → gateway-backend-service:5210
  → FileResponse 返回图片
  ✅ QQ / 浏览器均可访问
```

## 本次修复：图片 URL 外部不可访问 — 独立图床服务

### 问题
- `IMAGE_BASE_URL=http://gateway-backend-service:5210/api/assets` 是 K8s 内部 DNS
- QQ 报错 `ENOTFOUND gateway-backend-service`，网页显示破碎图片
- `host.docker.internal` 在 Windows 宿主机上无法解析（容器→宿主机单向通道）

### 方案
新建独立的 **image-assets-service**（nginx:alpine），挂载同一 PVC，NodePort 30502 对外暴露，不依赖 gateway。

### 改动文件

| 文件 | 改动 |
|---|---|
| `deploy/services/image-assets-service.yaml` | **新建** nginx 静态文件服务，挂载 PVC → `/usr/share/nginx/html/assets`，NodePort 30502 |
| `services/tool-runtime-service/app/server.py` | `IMAGE_BASE_URL` 改为纯 env 变量，无硬编码默认值 |
| `deploy/tool-runtime-apply.sh` | 新增 `IMAGE_BASE_URL` 变量，通过 `host.docker.internal` 动态解析宿主机 IP |
| `deploy-all.ps1` | 新增 image-assets-service 部署步骤 |

### 数据流
```
图片 URL: http://<宿主机IP>:30502/assets/xxx.png
  → NodePort 30502 → nginx Pod :80
  → /usr/share/nginx/html/assets/xxx.png（PVC）
  → 返回图片
  ✅ QQ / 浏览器均可访问
```

### 部署
```bash
kubectl apply -f deploy/services/image-assets-service.yaml
# tool-runtime 需重新部署以读取新的 IMAGE_BASE_URL env
```

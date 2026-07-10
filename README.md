# My_Agent_MSA
/mnt/e/github/My_Agent_MSA

下面是目前整个 My_Agent 微服务架构里约定的端口列表，后续 YAML / Dockerfile / Service 都按这个来。

模块    Service / Pod 名    端口    协议 / 用途
前端静态服务    frontend-service / frontend-web    80    HTTP，提供 login.html、chat.html、静态资源
Web 后端网关    gateway-backend-service    5210    HTTP，前端调用 /api/login、/api/messages、/api/events
任务调度服务    task-scheduler-service    5100    gRPC，接收 CreateTask，推送 TaskEvent
日志服务，可选    log-service    5101    HTTP / gRPC，可选，继承原 log.port
资源 / 图片服务    asset-service    5102    HTTP，提供图片、文件、上传产物
定时任务服务    timer-task-service    5103    gRPC，管理定时任务，到期回调 scheduler
用户服务    user-service    5104    gRPC，用户信息与多渠道绑定
QQ 网关    qq-gateway-service    5200    HTTP / WebSocket，QQ 渠道入口
Satori 适配器    qq-satori-adapter / 外部 Satori    5600    HTTP / WebSocket，QQ Satori 侧适配服务
Agent 编排服务    agent-orchestrator-service    5300    gRPC，执行任务、编排上下文/模型/工具
OpenViking 上下文服务    openviking-context-service    5301    gRPC，管理上下文、会话、检索
模型代理服务    model-proxy-service    5302    gRPC，统一模型调用适配
工具运行服务    tool-runtime-service    5303    gRPC，执行工具、skill、workspace 操作
模型服务    model-serving    8000    HTTP，OpenAI-compatible / Ollama / vLLM 适配端口


当前主要调用链端口
Browser
  ↓ HTTP
frontend-service:80
  ↓ /api/*
gateway-backend-service:5210
  ↓ gRPC
task-scheduler-service:5100
  ↓ gRPC
agent-orchestrator-service:5300
  ├─ openviking-context-service:5301
  ├─ model-proxy-service:5302
  │   └─ model-serving:8000
  ├─ tool-runtime-service:5303
  │   └─ asset-service:5102
  └─ timer-task-service:5103
      └─ task-scheduler-service:5100（到期回调）
Istio / 本地访问端口

如果通过 Istio 暴露：

组件    端口
istio-ingressgateway HTTP    80
本地 port-forward 推荐    localhost:8080 -> istio-ingressgateway:80

命令：

kubectl -n istio-system port-forward svc/istio-ingressgateway 8080:80

浏览器访问：

http://localhost:8080/login.html
http://localhost:8080/chat.html
Istio 路由对应关系
/login.html /chat.html /静态文件
  -> frontend-service:80

/api/events
  -> gateway-backend-service:5210

/api/
  -> gateway-backend-service:5210

/assets/
  -> asset-service:5102
NFS 相关

NFS 不是业务微服务端口，但底层挂载通常会用：

服务    端口
NFS    2049

你的 K8s PV 里不需要手动写 2049，只要写：

nfs:
  server: 172.29.219.49
  path: /srv/nfs/my-agent/workspace

Kubernetes 会按 NFS 协议挂载。
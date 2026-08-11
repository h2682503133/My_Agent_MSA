# ============================================================
# My_Agent_MSA 一键部署脚本
# 在项目根目录 PowerShell 中运行
# ============================================================

$ErrorActionPreference = "Stop"

# ─── 镜像版本配置 ───────────────────────────────────────────
$IMAGES = @{
    "dashboard-service"            = @{ dir = "services/dashboard-service";            tag = "v8"  }
    "agent-orchestrator-service"   = @{ dir = "services/agent-orchestrator-service";   tag = "v16" }
    "task-scheduler-service"       = @{ dir = "services/task-scheduler-service";       tag = "v6"  }
    "timer-task-service"           = @{ dir = "services/timer-task-service";           tag = "v2"  }
    "gateway-backend-service"      = @{ dir = "services/gateway-backend-service";      tag = "v22"  }
    "qq-llbot-service"             = @{ dir = "services/qq-llbot-service";             tag = "v1"  }
    "model-proxy-service"          = @{ dir = "services/model-proxy-service";          tag = "v3"  }
    "openviking-context-service"   = @{ dir = "services/openviking-context-service";   tag = "v20" }
    "tool-runtime-service"         = @{ dir = "services/tool-runtime-service";         tag = "v26"  }
    "user-service"                 = @{ dir = "services/user-service";                 tag = "v1"  }
    "frontend-service"             = @{ dir = "services/frontend-service";             tag = "v10"  }
}

# 服务名 → YAML 文件映射
$YAML_MAP = @{
    "dashboard-service"            = "deploy/services/dashboard-service.yaml"
    "agent-orchestrator-service"   = "deploy/services/agent-orchestrator-service.yaml"
    "task-scheduler-service"       = "deploy/services/task-scheduler-service.yaml"
    "timer-task-service"           = "deploy/services/timer-task-service.yaml"
    "gateway-backend-service"      = "deploy/services/gateway-backend-service.yaml"
    "qq-llbot-service"             = "deploy/services/qq-llbot-service.yaml"
    "model-proxy-service"          = "deploy/services/model-proxy-service.yaml"
    "openviking-context-service"   = "deploy/services/openviking-context-service.yaml"
    "openviking-server"            = "deploy/services/openviking-server.yaml"
    "user-service"                 = "deploy/services/user-service.yaml"
    "frontend-service"             = "deploy/services/frontend-service.yaml"
}

# tool-runtime 使用特殊部署脚本
$TOOL_RUNTIME_YAML = "deploy/services/tool-runtime-service.yaml"

# ─── 辅助函数 ───────────────────────────────────────────────

function Write-Step {
    param([string]$Text)
    Write-Host "`n>>> $Text" -ForegroundColor Cyan
}

function Write-OK {
    param([string]$Text)
    Write-Host "  [OK] $Text" -ForegroundColor Green
}

function Write-Warn {
    param([string]$Text)
    Write-Host "  [WARN] $Text" -ForegroundColor Yellow
}

# ─── 检查前置条件 ───────────────────────────────────────────

Write-Step "检查前置条件"

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-Host "[ERROR] 找不到 docker，请安装 Docker Desktop" -ForegroundColor Red
    exit 1
}
Write-OK "docker 已就绪"

if (-not (Get-Command kubectl -ErrorAction SilentlyContinue)) {
    Write-Host "[ERROR] 找不到 kubectl，请安装或启用 Docker Desktop 的 Kubernetes" -ForegroundColor Red
    exit 1
}
Write-OK "kubectl 已就绪"

# 检查 kubectl 能否连接集群
$clusterCheck = kubectl cluster-info 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "[ERROR] kubectl 无法连接集群，请确认 Docker Desktop Kubernetes 已启动" -ForegroundColor Red
    exit 1
}
Write-OK "K8s 集群已连接"

# ─── 交互式服务选择 ─────────────────────────────────────────

Write-Step "选择要部署的服务"

$serviceNames = $IMAGES.Keys | Sort-Object
$selected = @{}

# 默认全选
foreach ($svc in $serviceNames) { $selected[$svc] = $true }

# 多选界面
$cursorIdx = 0
$svcArray = @($serviceNames)

function Draw-Selection {
    Clear-Host
    Write-Host "`n  选择要部署的服务（↑↓ 移动  Space 选择  Enter 确认  Q 退出）`n" -ForegroundColor Cyan
    for ($i = 0; $i -lt $svcArray.Count; $i++) {
        $svc = $svcArray[$i]
        $tag = $IMAGES[$svc].tag
        $check = if ($selected[$svc]) { "[✓]" } else { "[ ]" }
        $prefix = if ($i -eq $cursorIdx) { " >" } else { "  " }
        $color = if ($i -eq $cursorIdx) { "Cyan" } else { "White" }
        if (-not $selected[$svc]) { $color = "DarkGray" }
        Write-Host "$prefix $check $svc ($tag)" -ForegroundColor $color
    }
    Write-Host "`n  提示：↑↓ 移动光标  Space 切换选中  Enter 确认  Q 退出" -ForegroundColor DarkGray
}

Draw-Selection
while ($true) {
    $key = [Console]::ReadKey($true)
    switch ($key.Key) {
        UpArrow    { $cursorIdx = [Math]::Max(0, $cursorIdx - 1); Draw-Selection }
        DownArrow  { $cursorIdx = [Math]::Min($svcArray.Count - 1, $cursorIdx + 1); Draw-Selection }
        Spacebar   { $svc = $svcArray[$cursorIdx]; $selected[$svc] = -not $selected[$svc]; Draw-Selection }
        Q          { Write-Host "`n已取消。" -ForegroundColor Yellow; exit 0 }
        Enter      { break }
    }
    if ($key.Key -eq "Enter") { break }
}

$toDeploy = @()
foreach ($svc in $serviceNames) {
    if ($selected[$svc]) { $toDeploy += $svc }
}

if ($toDeploy.Count -eq 0) {
    Write-Host "`n没有选择任何服务，退出。" -ForegroundColor Yellow
    exit 0
}

Write-Host "`n将要部署：" -ForegroundColor Yellow -NoNewline
Write-Host " $($toDeploy -join ', ')" -ForegroundColor White

# ─── Docker 构建 ────────────────────────────────────────────

Write-Step "Docker 构建"

foreach ($svc in $toDeploy) {
    $dir = $IMAGES[$svc].dir
    $tag = $IMAGES[$svc].tag
    $image = "agent/$($svc):$tag"

    if (-not (Test-Path $dir)) {
        Write-Warn "$dir 目录不存在，跳过 $svc"
        continue
    }
    if (-not (Test-Path "$dir/Dockerfile")) {
        Write-Warn "$dir/Dockerfile 不存在，跳过 $svc"
        continue
    }

    Write-Host "  构建 $image ..."
    docker build -t $image $dir
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  [FAIL] $svc 构建失败" -ForegroundColor Red
        exit 1
    }
    Write-OK "$svc 构建完成"
}

# ─── K8s 部署 ───────────────────────────────────────────────

Write-Step "K8s 部署"

# 确保 namespace 存在
kubectl create namespace agent --dry-run=client -o yaml | kubectl apply -f -

# RBAC: 允许 default SA 读取 Pod 日志（gateway-backend-service 日志接口需要）
kubectl -n agent create role pod-log-reader --verb=get,list --resource=pods,namespaces,pods/log --dry-run=client -o yaml | kubectl apply -f -
kubectl -n agent create rolebinding pod-log-reader-binding --role=pod-log-reader --serviceaccount=agent:default --dry-run=client -o yaml | kubectl apply -f -

foreach ($svc in $toDeploy) {
    $yamlFile = $YAML_MAP[$svc]
    if (-not $yamlFile) {
        Write-Warn "未找到 $svc 的 YAML 映射，跳过"
        continue
    }
    if (-not (Test-Path $yamlFile)) {
        Write-Warn "$yamlFile 不存在，跳过 $svc"
        continue
    }

    Write-Host "  部署 $svc ..."
    kubectl apply -f $yamlFile
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  [FAIL] $svc 部署失败" -ForegroundColor Red
        exit 1
    }
    Write-OK "$svc 已部署"
}

# ─── 图床服务（nginx 静态文件，无需构建）─────────────────────

Write-Step "部署 image-assets-service"
kubectl apply -f deploy/services/image-assets-service.yaml
if ($LASTEXITCODE -ne 0) {
    Write-Host "  [FAIL] image-assets-service 部署失败" -ForegroundColor Red
    exit 1
}
Write-OK "image-assets-service 已部署"

# ─── 完成 ───────────────────────────────────────────────────

Write-Host "`n========================================" -ForegroundColor Green
Write-Host "  部署完成！" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Green

Write-Host "`n检查 Pod 状态："
kubectl -n agent get pods

Write-Host "`n检查 Service："
kubectl -n agent get svc

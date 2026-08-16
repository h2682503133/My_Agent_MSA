# ============================================================
# My_Agent_MSA 一键部署脚本
# 在项目根目录 PowerShell 中运行
# ============================================================

$ErrorActionPreference = "Stop"

# ─── 镜像配置 ───────────────────────────────────────────────
# 版本号不在此维护：构建时从 deploy/services/*.yaml 的 image 字段自动解析（见 Get-ImageTag），
# tool-runtime 无 YAML，版本从 deploy/tool-runtime-apply.sh 的 TOOL_RUNTIME_IMAGE 解析。
# 外部镜像（openviking-server / image-assets-service）无需本地构建（build=$false）。
$IMAGES = @{
    "dashboard-service"            = @{ dir = "services/dashboard-service" }
    "agent-orchestrator-service"   = @{ dir = "services/agent-orchestrator-service" }
    "task-scheduler-service"       = @{ dir = "services/task-scheduler-service" }
    "timer-task-service"           = @{ dir = "services/timer-task-service" }
    "gateway-backend-service"      = @{ dir = "services/gateway-backend-service" }
    "qq-llbot-service"             = @{ dir = "services/qq-llbot-service" }
    "model-proxy-service"          = @{ dir = "services/model-proxy-service" }
    "openviking-context-service"   = @{ dir = "services/openviking-context-service" }
    "tool-runtime-service"         = @{ dir = "services/tool-runtime-service" }
    "user-service"                 = @{ dir = "services/user-service" }
    "frontend-service"             = @{ dir = "services/frontend-service" }
    "openviking-server"            = @{ dir = "external"; build = $false }
    "image-assets-service"         = @{ dir = "external"; build = $false }
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
    "image-assets-service"         = "deploy/services/image-assets-service.yaml"
}

# tool-runtime 使用特殊部署脚本（外部 VM + OpenViking + searxng，需在 WSL 中执行）
$TOOL_RUNTIME_SCRIPT = "deploy/tool-runtime-apply.sh"

# ─── 必要服务（Web 对话最小闭环）────────────────────────────
# 勾选「必要服务」即一次选中以下全部；其余服务按需单独勾选。
$CORE_SERVICES = @(
    "frontend-service",
    "gateway-backend-service",
    "task-scheduler-service",
    "agent-orchestrator-service",
    "model-proxy-service"
)

# ─── 选择列表 ───────────────────────────────────────────────
# type: group   = 组合选项（一次选中多个服务）
#       single  = 单个服务
#       feature = tool-runtime 的功能子项（codex / clawhub），仅在 tool-runtime 勾选时生效
$OPTIONS = @(
    @{ key = "core";                type = "group";   label = "必要服务（Web 对话最小闭环）";               services = $CORE_SERVICES }
    @{ key = "timer-task-service";  type = "single";  label = "timer-task-service 定时任务";                 service = "timer-task-service" }
    @{ key = "qq-llbot-service";    type = "single";  label = "qq-llbot-service QQ 渠道";                    service = "qq-llbot-service" }
    @{ key = "openviking-context-service"; type = "single"; label = "openviking-context-service 长期记忆(RAG,自动带server)"; service = "openviking-context-service" }
    @{ key = "openviking-server";   type = "single";  label = "openviking-server 语义检索库";                service = "openviking-server" }
    @{ key = "user-service";        type = "single";  label = "user-service 用户信息";                       service = "user-service" }
    @{ key = "dashboard-service";   type = "single";  label = "dashboard-service 管理面板";                  service = "dashboard-service" }
    @{ key = "tool-runtime-service"; type = "single"; label = "tool-runtime-service 工具执行(需WSL)";        service = "tool-runtime-service" }
    @{ key = "codex";               type = "feature"; label = "  └ codex 代码生成";                          parent = "tool-runtime-service" }
    @{ key = "clawhub";             type = "feature"; label = "  └ clawhub 技能执行";                        parent = "tool-runtime-service" }
    @{ key = "image-assets-service"; type = "single"; label = "image-assets-service 图床";                   service = "image-assets-service" }
)

# ─── 服务依赖 ───────────────────────────────────────────────
# 勾选某服务时自动补齐其依赖（展开阶段处理，缺失时警告并自动加入）
$DEPENDENCIES = @{
    "openviking-context-service" = @("openviking-server")
}

# ─── 服务 → 所需配置映射 ────────────────────────────────────
# 部署时只把各服务自己需要的配置同步到 NFS（相对 config/ 的路径）。
# 路径均相对于仓库根；目录以 / 结尾表示整个目录。
$CONFIG_MAP = @{
    "agent-orchestrator-service" = @(
        "config/orchestrator/config/agent_list.json",
        "config/orchestrator/config/system_settings.json",
        "config/orchestrator/system_prompt/"
    )
    "model-proxy-service" = @(
        "config/model-proxy/config/model_list.json"
    )
    "openviking-server" = @(
        "config/openviking/ov.conf"
    )
    "openviking-context-service" = @(
        "config/openviking/root_api_key",
        "config/openviking/api_key"
    )
    "qq-llbot-service" = @(
        "config/qq-llbot/qq_llbot_config.json"
    )
    "tool-runtime-service" = @(
        "config/openviking/api_key"
    )
    # dashboard 为管理面板，浏览/编辑整个配置目录
    "dashboard-service" = @(
        "config/orchestrator/config/agent_list.json",
        "config/orchestrator/config/system_settings.json",
        "config/orchestrator/system_prompt/",
        "config/model-proxy/config/model_list.json",
        "config/openviking/ov.conf",
        "config/openviking/root_api_key",
        "config/openviking/api_key",
        "config/qq-llbot/qq_llbot_config.json"
    )
}

# ─── 从部署文件解析镜像 tag ─────────────────────────────────
# 版本号只维护在 deploy/services/*.yaml 的 image 字段中；
# tool-runtime 无 YAML，版本从 deploy/tool-runtime-apply.sh 的 TOOL_RUNTIME_IMAGE 解析。
function Get-ImageTag {
    param([string]$Name)

    # 与脚本其余部分一致，使用相对路径（脚本假定从项目根目录运行）
    if ($Name -eq "tool-runtime-service") {
        if (-not (Test-Path $TOOL_RUNTIME_SCRIPT)) { return "" }
        $m = [regex]::Match((Get-Content $TOOL_RUNTIME_SCRIPT -Raw), 'TOOL_RUNTIME_IMAGE[^:]*:\s*[^:]+:([^\s"}]+)')
        if ($m.Success) { return $m.Groups[1].Value }
        return ""
    }

    $yamlFile = $YAML_MAP[$Name]
    if (-not (Test-Path $yamlFile)) { return "" }
    $line = [regex]::Match((Get-Content $yamlFile -Raw), '(?m)^\s*image:\s*(\S+)\s*$').Groups[1].Value
    if (-not $line) { return "" }
    $parts = $line.Split(':')
    return $parts[$parts.Length - 1]
}

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

# ─── 同步配置到 NFS（只复制各服务需要的配置）────────────────
function Sync-ConfigToNfs {
    param([string[]]$Services)

    if (-not (Get-Command wsl -ErrorAction SilentlyContinue)) {
        Write-Warn "未检测到 WSL，跳过配置同步。请手动执行：bash deploy/sync-config.sh"
        return
    }

    $nfsRoot = if ($env:NFS_ROOT) { $env:NFS_ROOT } else { "/srv/nfs/my-agent" }
    $drive = (Get-Location).Path.Substring(0, 1).ToLower()
    $repoWsl = "/mnt/$drive" + ((Get-Location).Path.Substring(2) -replace "\\", "/")

    foreach ($svc in $Services) {
        $srcs = $CONFIG_MAP[$svc]
        if (-not $srcs) { continue }
        foreach ($src in $srcs) {
            $rel = $src -replace "\\", "/"
            $trimmed = $rel.TrimEnd("/")
            $lastSlash = $trimmed.LastIndexOf("/")
            $destDir = if ($lastSlash -gt 0) { "$nfsRoot/" + $trimmed.Substring(0, $lastSlash) } else { $nfsRoot }
            $cmd = "mkdir -p '$destDir' && cp -r '$repoWsl/$trimmed' '$destDir/' 2>/dev/null || true"
            Write-Host "  [sync] $svc -> $rel"
            wsl -e bash -lc $cmd
        }
    }
    Write-OK "配置已同步到 NFS: $nfsRoot/config/"
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

$selected = @{}
foreach ($opt in $OPTIONS) { $selected[$opt.key] = $false }
# 默认勾选必要服务（最小闭环），其余不选
$selected["core"] = $true

$cursorIdx = 0

function Draw-Selection {
    Clear-Host
    Write-Host ""
    Write-Host "  选择要部署的服务（↑↓ 移动  Space 选择/取消  Enter 确认  Q 退出）" -ForegroundColor Cyan
    Write-Host "  「必要服务」= Web 对话最小闭环；其余按需勾选。codex / clawhub 需先勾选 tool-runtime-service" -ForegroundColor DarkGray
    Write-Host ""
    for ($i = 0; $i -lt $OPTIONS.Count; $i++) {
        $opt = $OPTIONS[$i]
        $isOn = $selected[$opt.key]
        $locked = ($opt.type -eq "feature" -and -not $selected[$opt.parent])
        $check = if ($isOn) { "[✓]" } else { "[ ]" }
        if ($locked) { $check = "[·]" }

        $prefix = if ($i -eq $cursorIdx) { " >" } else { "  " }
        $color = "DarkGray"
        if ($i -eq $cursorIdx) { $color = "Cyan" }
        elseif ($isOn) { $color = "Green" }

        $tagPart = ""
        if ($opt.type -eq "single") {
            $t = Get-ImageTag $opt.service
            if ($t) { $tagPart = "  ($t)" }
        } elseif ($opt.type -eq "group") {
            $tagPart = "  (×$($opt.services.Count))"
        }

        Write-Host "$prefix $check $($opt.label)$tagPart" -ForegroundColor $color
    }
    Write-Host ""
    Write-Host "  提示：↑↓ 移动  Space 切换  Enter 确认  Q 退出" -ForegroundColor DarkGray
}

Draw-Selection
while ($true) {
    $key = [Console]::ReadKey($true)
    switch ($key.Key) {
        UpArrow   {
            $cursorIdx = [Math]::Max(0, $cursorIdx - 1)
            Draw-Selection
        }
        DownArrow {
            $cursorIdx = [Math]::Min($OPTIONS.Count - 1, $cursorIdx + 1)
            Draw-Selection
        }
        Spacebar  {
            $opt = $OPTIONS[$cursorIdx]
            # codex/clawhub 在 tool-runtime 未勾选时锁定
            if ($opt.type -eq "feature" -and -not $selected[$opt.parent]) {
                Draw-Selection
                break
            }
            $selected[$opt.key] = -not $selected[$opt.key]
            # 关闭单个服务时，若它是 feature 的父项，同步关闭其子项
            if ($opt.type -eq "single" -and -not $selected[$opt.key]) {
                foreach ($f in $OPTIONS) {
                    if ($f.type -eq "feature" -and $f.parent -eq $opt.key) { $selected[$f.key] = $false }
                }
            }
            Draw-Selection
        }
        Q         { Write-Host "`n已取消。" -ForegroundColor Yellow; exit 0 }
        Enter     { break }
    }
    if ($key.Key -eq "Enter") { break }
}

# ─── 展开选择结果 ───────────────────────────────────────────

$toDeploy = @()
$toolRuntimeFeatures = @{ codex = $false; clawhub = $false }

foreach ($opt in $OPTIONS) {
    if (-not $selected[$opt.key]) { continue }
    switch ($opt.type) {
        "group"   {
            foreach ($s in $opt.services) {
                if ($s -notin $toDeploy) { $toDeploy += $s }
            }
        }
        "single"  {
            if ($opt.service -notin $toDeploy) { $toDeploy += $opt.service }
        }
        "feature" { $toolRuntimeFeatures[$opt.key] = $true }
    }
}

# 补齐依赖：勾选了依赖方但未勾选其依赖项时，自动加入并提示
foreach ($svc in @($toDeploy)) {
    foreach ($dep in $DEPENDENCIES[$svc]) {
        if ($dep -and $dep -notin $toDeploy) {
            Write-Warn "$svc 依赖 $dep，已自动加入部署列表"
            $toDeploy += $dep
        }
    }
}

if ($toDeploy.Count -eq 0) {
    Write-Host "`n没有选择任何服务，退出。" -ForegroundColor Yellow
    exit 0
}

Write-Host "`n将要部署：" -ForegroundColor Yellow -NoNewline
Write-Host " $($toDeploy -join ', ')" -ForegroundColor White
if ($toolRuntimeFeatures["codex"] -or $toolRuntimeFeatures["clawhub"]) {
    $feats = @()
    if ($toolRuntimeFeatures["codex"]) { $feats += "codex" }
    if ($toolRuntimeFeatures["clawhub"]) { $feats += "clawhub" }
    Write-Host "  tool-runtime 功能: $($feats -join ', ')" -ForegroundColor DarkGray
}

# ─── openviking 配置检查 ────────────────────────────────────
# openviking-server / context-service 依赖 config/openviking/ 下的配置：
#   ov.conf（embedding/vlm 需手动填写）、root_api_key（项目固定值）
#   api_key 无需预存在：部署后的「初始化 openviking」步骤会自动创建 agent-service 用户并生成。
# 配置需先同步到 NFS（bash deploy/sync-config.sh），Pod 才能正常启动。
if ($toDeploy -contains "openviking-server" -or $toDeploy -contains "openviking-context-service") {
    Write-Step "检查 openviking 配置"
    $ovFiles = @("config/openviking/ov.conf", "config/openviking/root_api_key")
    $missing = @()
    foreach ($f in $ovFiles) {
        if (-not (Test-Path $f)) { $missing += $f }
    }
    if ($missing.Count -gt 0) {
        Write-Warn "openviking 配置文件缺失: $($missing -join ', ')"
        Write-Warn "请创建并填写这些文件（ov.conf 需手动填写 embedding/vlm 的 API 配置），然后执行：bash deploy/sync-config.sh"
    } elseif ((Get-Content "config/openviking/ov.conf" -Raw -ErrorAction SilentlyContinue) -match "replace-with") {
        Write-Warn "config/openviking/ov.conf 中仍有 replace-with 占位（vlm.api_key 等未填写）"
        Write-Warn "请手动填写后执行：bash deploy/sync-config.sh 同步到 NFS"
    } else {
        Write-OK "openviking 配置已就绪（config/openviking/），确认已同步到 NFS（bash deploy/sync-config.sh）"
    }
}

# ─── Docker 构建 ────────────────────────────────────────────

Write-Step "Docker 构建"

foreach ($svc in $toDeploy) {
    $dir = $IMAGES[$svc].dir

    # 外部镜像（如 openviking-server / image-assets-service）跳过本地构建
    if ($IMAGES[$svc].build -eq $false) {
        $tag = Get-ImageTag $svc
        Write-OK "$svc 使用外部镜像 agent/$($svc):$tag，跳过构建"
        continue
    }

    if (-not (Test-Path $dir)) {
        Write-Warn "$dir 目录不存在，跳过 $svc"
        continue
    }
    if (-not (Test-Path "$dir/Dockerfile")) {
        Write-Warn "$dir/Dockerfile 不存在，跳过 $svc"
        continue
    }

    # 版本号从部署文件自动解析，保证构建与部署的镜像 tag 一致
    $tag = Get-ImageTag $svc
    if (-not $tag) {
        Write-Warn "无法从部署文件解析 $svc 的镜像 tag，跳过构建"
        continue
    }
    $image = "agent/$($svc):$tag"

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

# 同步各服务需要的配置到 NFS（只复制 $CONFIG_MAP 中列出的文件）
Sync-ConfigToNfs $toDeploy

# 确保 namespace 存在
kubectl create namespace agent --dry-run=client -o yaml | kubectl apply -f -

# RBAC: 允许 default SA 读取 Pod 日志（部分服务日志接口需要）
kubectl -n agent create role pod-log-reader --verb=get,list --resource=pods,pods/log --dry-run=client -o yaml | kubectl apply -f -
kubectl -n agent create rolebinding pod-log-reader-binding --role=pod-log-reader --serviceaccount=agent:default --dry-run=client -o yaml | kubectl apply -f -

foreach ($svc in $toDeploy) {
    # tool-runtime 使用特殊部署脚本（外部 VM 执行环境 + OpenViking + searxng）
    if ($svc -eq "tool-runtime-service") {
        Write-Host "`n  部署 tool-runtime-service（特殊脚本 $TOOL_RUNTIME_SCRIPT）..."
        $featClawhub   = if ($toolRuntimeFeatures["clawhub"]) { "true" } else { "false" }
        $featCodex     = if ($toolRuntimeFeatures["codex"])   { "true" } else { "false" }
        # 技能知识库依赖 OpenViking：未勾选 openviking-server 时关闭（相关工具调用会提示未启用）
        $featOpenviking = if ($toDeploy -contains "openviking-server") { "true" } else { "false" }
        if (Get-Command wsl -ErrorAction SilentlyContinue) {
            $drive = (Get-Location).Path.Substring(0, 1).ToLower()
            $wslPath = "/mnt/$drive" + ((Get-Location).Path.Substring(2) -replace "\\", "/")
            Write-Host "  [INFO] 通过 WSL 执行：cd $wslPath && ENABLE_CLAWHUB=$featClawhub ENABLE_CODEX=$featCodex ENABLE_OPENVIKING=$featOpenviking bash $TOOL_RUNTIME_SCRIPT" -ForegroundColor DarkGray
            wsl -e bash -lc "cd '$wslPath' && ENABLE_CLAWHUB=$featClawhub ENABLE_CODEX=$featCodex ENABLE_OPENVIKING=$featOpenviking bash $TOOL_RUNTIME_SCRIPT"
            if ($LASTEXITCODE -ne 0) {
                Write-Warn "tool-runtime-service 部署未完成（可能需要 sudo 密码或环境依赖），请手动在 WSL 中执行：bash $TOOL_RUNTIME_SCRIPT"
                continue
            }
            Write-OK "tool-runtime-service 已部署"
        } else {
            Write-Warn "未检测到 WSL，跳过 tool-runtime-service。请手动在 WSL 中执行：bash $TOOL_RUNTIME_SCRIPT"
        }
        continue
    }

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

# ─── openviking 初始化（创建 agent-service 用户）─────────────
# openviking-server 部署完成后，用 root_api_key 调 admin API 创建 agent-service 用户，
# 将返回的 user_key 写入 config/openviking/api_key（供 tool-runtime / context-service 共用
# 的技能账户）。首次需手动填写 ov.conf 并同步 NFS，否则 Pod 不会就绪。
if ($toDeploy -contains "openviking-server") {
    Write-Step "初始化 openviking（创建 agent-service 用户）"

    $rootKeyFile = "config/openviking/root_api_key"
    $apiKeyFile  = "config/openviking/api_key"
    if (-not (Test-Path $rootKeyFile)) {
        Write-Warn "缺少 $rootKeyFile，跳过 openviking 初始化"
    } else {
        $rootKey = (Get-Content $rootKeyFile -Raw).Trim()
        if (-not $rootKey) {
            Write-Warn "root_api_key 为空，跳过 openviking 初始化"
        } else {
            # 等待 openviking Pod ready（ov.conf 未配置好时可能超时）
            Write-Host "  等待 openviking Pod ready ..."
            kubectl -n agent wait --for=condition=ready pod -l app=openviking --timeout=180s
            if ($LASTEXITCODE -ne 0) {
                Write-Warn "openviking Pod 未就绪：请检查 config/openviking/ov.conf 的 embedding/vlm 配置，并执行 bash deploy/sync-config.sh 同步到 NFS"
            } else {
                # port-forward 到本地，调 admin API 创建 agent-service 用户
                $pf = Start-Job -ScriptBlock { kubectl -n agent port-forward svc/openviking 1933:1933 }
                try {
                    Start-Sleep -Seconds 3
                    $headers = @{ "X-API-Key" = $rootKey }
                    $body    = '{"user_id":"agent-service","role":"user"}'
                    try {
                        $resp = Invoke-RestMethod -Uri "http://127.0.0.1:1933/api/v1/admin/accounts/my-agent/users" `
                            -Method Post -Headers $headers -ContentType "application/json" -Body $body -TimeoutSec 15
                        $userKey = $resp.result.user_key
                        if (-not $userKey) { $userKey = $resp.user_key }
                        if ($userKey) {
                            [System.IO.File]::WriteAllText(
                                (Join-Path (Get-Location) $apiKeyFile),
                                $userKey,
                                (New-Object System.Text.UTF8Encoding($false)))
                            Write-OK "agent-service 用户已创建，key 已写入 $apiKeyFile"
                            # 自动把新 key 同步到 NFS，并重启已部署的相关服务以读取
                            Sync-ConfigToNfs @("openviking-context-service", "tool-runtime-service", "dashboard-service")
                            if ($toDeploy -contains "openviking-context-service") {
                                Write-Host "  [restart] openviking-context-service ..."
                                kubectl -n agent rollout restart deployment/openviking-context-service
                            }
                            if ($toDeploy -contains "tool-runtime-service") {
                                Write-Host "  [restart] tool-runtime-service ..."
                                kubectl -n agent rollout restart deployment/tool-runtime-service
                            }
                        } else {
                            Write-Warn "创建 agent-service 用户成功但响应中没有 user_key: $($resp | ConvertTo-Json -Compress)"
                        }
                    } catch {
                        $status = $null
                        try { $status = [int]$_.Exception.Response.StatusCode } catch {}
                        if ($status -eq 409) {
                            Write-Warn "agent-service 用户已存在（409），保留现有 $apiKeyFile"
                        } else {
                            Write-Warn "创建 agent-service 用户失败: $($_.Exception.Message)"
                        }
                    }
                } finally {
                    Stop-Job $pf -ErrorAction SilentlyContinue
                    Remove-Job $pf -ErrorAction SilentlyContinue
                }
            }
        }
    }
}

# ─── 完成 ───────────────────────────────────────────────────

Write-Host "`n========================================" -ForegroundColor Green
Write-Host "  部署完成！" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Green

Write-Host "`n检查 Pod 状态："
kubectl -n agent get pods

Write-Host "`n检查 Service："
kubectl -n agent get svc

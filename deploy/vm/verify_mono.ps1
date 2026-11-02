# 单机（Windows、无 Docker）端到端自检：在虚拟机里跑，结果回传宿主
# 用法：powershell -NoProfile -ExecutionPolicy Bypass -File verify_mono.ps1 -HostBase http://10.244.37.37:8000 -UserId nh

param(
    [string]$HostBase = "http://10.244.37.37:8000",
    [string]$UserId = "nh",
    [string]$DataDir = "$env:LOCALAPPDATA\MyAgent\data"
)

$ErrorActionPreference = "Continue"
$results = [ordered]@{}
function Note($key, $value) { $results[$key] = $value }
function Http($url) {
    try { return (Invoke-WebRequest $url -TimeoutSec 10 -UseBasicParsing).Content } catch { return "ERR: $($_.Exception.Message)" }
}
function Code($url) {
    try { return (Invoke-WebRequest $url -TimeoutSec 10 -UseBasicParsing -Method Head).StatusCode } catch { return "ERR" }
}

# 1) 服务与端口
$pids = "$DataDir\pids.json"
if (Test-Path $pids) {
    $state = Get-Content $pids -Raw | ConvertFrom-Json
    Note "services" = ($state.processes | ForEach-Object { "$($_.id):$($_.pid)" }) -join ", "
    Note "service_count" $state.processes.Count
} else {
    Note "services" "pids.json 不存在"
}
foreach ($pair in @(@('8080','web'), @('5700','dashboard'), @('5102','assets'), @('5210','gateway'))) {
    $port = [int]$pair[0]
    $tcp = Test-NetConnection -ComputerName 127.0.0.1 -Port $port -InformationLevel Quiet -WarningAction SilentlyContinue
    Note ("port_" + $pair[1]) $tcp
}

# 2) 工具权限接口（读）
$tp = Http "http://127.0.0.1:5700/api/tool_permissions"
if ($tp -notlike "ERR:*") {
    try {
        $d = $tp | ConvertFrom-Json
        Note "perm_L2" ($d.L2_whitelist_users -join ",")
        Note "perm_L3" ($d.L3_whitelist_users -join ",")
        Note "perm_L4" ($d.L4_whitelist_users -join ",")
        Note "perm_levels" $d.levels.Count
        Note "perm_tools" $d.tools.Count
        Note "perm_settings_path" $d.settings_path
    } catch { Note "perm_parse" $_.Exception.Message }
} else { Note "perm_api" $tp }

# 3) 前端脚本是否为新版（tpTools 冲突已修）
$js = "$env:LOCALAPPDATA\..\..\..\..\..\..\MyAgent"   # 占位，真实路径下面用仓库路径
$repoJs = "C:\MyAgent\services\dashboard-service\static\assets\js\toolperm.js"
if (Test-Path $repoJs) {
    $text = Get-Content $repoJs -Raw
    Note "js_has_tpPermTools" ($text -like "*tpPermTools*")
    Note "js_declares_tpTools" ($text -match "(?m)^let tpTools\b")
} else { Note "js_missing" $repoJs }

# 4) 工作空间：上传一个测试文件 → 列目录
$tmp = Join-Path $env:TEMP "mono-upload-test.txt"
Set-Content -Path $tmp -Value ("upload-check " + (Get-Date -Format "HH:mm:ss")) -Encoding UTF8
try {
    $up = curl.exe -s -X POST "http://127.0.0.1:5210/api/workspace/files/upload?user_id=$UserId&path=" -F "file=@$tmp;filename=mono-upload-test.txt"
    Note "upload_resp" ($up -join "")
} catch { Note "upload_error" $_.Exception.Message }

Start-Sleep -Seconds 1
$list = Http "http://127.0.0.1:5210/api/workspace/files?user_id=$UserId&path="
Note "workspace_list" ($list.Substring(0, [Math]::Min(400, $list.Length)))

# 5) 磁盘上是否真的落了文件
$expect = Join-Path $DataDir "workspace\users\$UserId\mono-upload-test.txt"
Note "file_on_disk" (Test-Path $expect)
Note "file_path" $expect

# 6) 图床
$asset = Http "http://127.0.0.1:5102/health"
Note "assets_health" $asset

# 回传宿主
$body = @{ stage = "mono-verify"; ok = $true; detail = ($results | ConvertTo-Json -Depth 4); host = $env:COMPUTERNAME } | ConvertTo-Json -Depth 5
try {
    Invoke-RestMethod -Uri "$HostBase/report" -Method Post -Body $body -ContentType "application/json; charset=utf-8" | Out-Null
    Write-Host "已回传宿主"
} catch { Write-Host "回传失败：$($_.Exception.Message)" }

$results | ConvertTo-Json -Depth 4

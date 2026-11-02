# 在 Windows 虚拟机里一键自测"单体模式（无 Docker）"
#
# 用法（在虚拟机里，PowerShell）：
#   iwr http://10.0.2.2:8000/bootstrap.ps1 -OutFile $env:TEMP\b.ps1
#   powershell -ExecutionPolicy Bypass -File $env:TEMP\b.ps1
#
# 它会：
#   1. 装 Python 3.11（用户级，静默）
#   2. 从宿主机下载项目压缩包并解压到 C:\MyAgent
#   3. 只探测环境（probe_env），确认档位是 T0、不需要 Docker
#   4. 建共享 venv 并安装依赖 + 生成 gRPC stubs
#   5. 把结果回传到宿主机（http://10.0.2.2:8000/report）

param(
    [string]$HostBase = "http://10.0.2.2:8000",
    [string]$Dest = "C:\MyAgent",
    [switch]$SkipDeps
)

$ErrorActionPreference = "Continue"
$log = Join-Path $env:TEMP "myagent-bootstrap.log"
function Log($msg) {
    $line = "[{0}] {1}" -f (Get-Date -Format "HH:mm:ss"), $msg
    Write-Host $line
    Add-Content -Path $log -Value $line -Encoding UTF8
}

function Report($stage, $ok, $detail) {
    $body = @{ stage = $stage; ok = $ok; detail = "$detail"; host = $env:COMPUTERNAME } | ConvertTo-Json -Depth 3
    try {
        Invoke-RestMethod -Uri "$HostBase/report" -Method Post -Body $body -ContentType "application/json; charset=utf-8" | Out-Null
    } catch { Log "回传失败（不影响本地测试）: $($_.Exception.Message)" }
}

Log "开始：$env:COMPUTERNAME"

# ── 1. Python ──────────────────────────────────────────────
# 注意：Windows 自带的 python.exe 是 Store 占位符（WindowsApps），必须跳过，
# 否则会得到 "Python was not found; run without arguments to install from Microsoft Store"
function Resolve-Python {
    foreach ($cand in @("python.exe", "python3.exe")) {
        $cmd = Get-Command $cand -ErrorAction SilentlyContinue
        if (-not $cmd) { continue }
        if ($cmd.Source -match "WindowsApps") { continue }
        try {
            $v = & $cmd.Source -V 2>&1
            if ($LASTEXITCODE -eq 0 -and "$v" -match "Python 3") { return $cmd.Source }
        } catch { }
    }
    return $null
}

$python = Resolve-Python
if (-not $python) {
    Log "未找到 Python，开始安装 3.11（用户级）"
    $installer = Join-Path $env:TEMP "python-3.11.9-amd64.exe"
    $sources = @("$HostBase/python.exe", "https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe")
    foreach ($src in $sources) {
        try {
            Log "下载 Python: $src"
            Invoke-WebRequest $src -OutFile $installer -UseBasicParsing
            Log ("下载完成: {0:N1} MB" -f ((Get-Item $installer).Length / 1MB))
            Start-Process -FilePath $installer -ArgumentList "/quiet","InstallAllUsers=0","PrependPath=1","Include_test=0" -Wait
            $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "User") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "Machine")
            $python = Resolve-Python
            if ($python) { break }
        } catch { Log "该源失败: $($_.Exception.Message)" }
    }
}
if (-not $python) {
    Log "没有 Python，终止"
    Report "python" $false "未安装成功"
    exit 1
}
Log "Python: $python"
Report "python" $true $python

# ── 2. 取项目 ─────────────────────────────────────────────
if (-not (Test-Path (Join-Path $Dest "deploy\features.json"))) {
    Log "下载项目压缩包"
    $zip = Join-Path $env:TEMP "repo.zip"
    try {
        Invoke-WebRequest "$HostBase/repo.zip" -OutFile $zip -UseBasicParsing
        Expand-Archive -Path $zip -DestinationPath $Dest -Force
        Log "解压完成 → $Dest"
    } catch {
        Log "下载/解压失败: $($_.Exception.Message)"
        Report "repo" $false $_.Exception.Message
        exit 1
    }
}
Report "repo" $true $Dest

# ── 3. 环境探测（应判为 T0）───────────────────────────────
$probe = Join-Path $Dest "services\agent-orchestrator-service\tools\probe_env.py"
$capPath = Join-Path $Dest "data\capability.json"
New-Item -ItemType Directory -Force -Path (Split-Path $capPath) | Out-Null
& $python -X utf8 $probe --out $capPath
if (Test-Path $capPath) {
    $cap = Get-Content $capPath -Raw | ConvertFrom-Json
    Log "档位=$($cap.runtime_tier) 降级=$($cap.degraded.Count) 项"
    Report "probe" $true "tier=$($cap.runtime_tier)"
} else {
    Report "probe" $false "没有生成 capability.json"
}

# ── 4. 依赖（共享 venv）──────────────────────────────────
if (-not $SkipDeps) {
    $deps = Join-Path $Dest "deploy\launcher\deps.py"
    Log "安装依赖（共享 venv，首次较慢）"
    & $python -X utf8 $deps --data-dir (Join-Path $Dest "data") --features core,memory,tools.basic --install
    $depsOk = ($LASTEXITCODE -eq 0)
    Log "依赖安装退出码=$LASTEXITCODE"
    Report "deps" $depsOk "exit=$LASTEXITCODE"
}

# ── 5. 汇总 ───────────────────────────────────────────────
$summary = @(
    "COMPUTERNAME=$env:COMPUTERNAME",
    "python=$python",
    "repo=$Dest",
    "log=$log"
) -join "`n"
Report "done" $true $summary
Log "完成。日志：$log"

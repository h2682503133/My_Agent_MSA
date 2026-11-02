# 逐条核对 部署指南.md 里可机械验证的声明（新用户视角）
$ErrorActionPreference = "Continue"
$data = Join-Path $env:LOCALAPPDATA "MyAgent\data"
$repo = "C:\MyAgent"
$lines = New-Object System.Collections.Generic.List[string]
function Say($m) { $lines.Add($m); Write-Host $m }

Say "== 4.0 前置 =="
$pv = (& python -V 2>&1) -join " "
Say ("  python -V → " + $pv)
Say ("  python 路径 → " + (Get-Command python).Source)
$disk = Get-PSDrive C | Select-Object -First 1
Say ("  C: 剩余 GB → " + [math]::Round($disk.Free/1GB,1))
$py311 = & python -c "import sys;print('311' if sys.version_info[:2]==(3,11) else str(sys.version_info[:2]))"
Say ("  版本判定 → " + $py311)

Say "== 4.0 数据目录结构 =="
foreach ($p in @("config","workspace","assets\images","logs","timer-tasks")) {
    Say ("  " + $p + " 存在 → " + (Test-Path (Join-Path $data $p)))
}

Say "== 4.x 运行态文件 =="
foreach ($p in @("pids.json","runtime.json","capability.json","launcher.lock")) {
    Say ("  " + $p + " 存在 → " + (Test-Path (Join-Path $data $p)))
}
$cap = Join-Path $data "capability.json"
if (Test-Path $cap) {
    $c = Get-Content $cap -Raw -Encoding UTF8 | ConvertFrom-Json
    Say ("  capability.runtime_tier → " + $c.runtime_tier)
    Say ("  capability.degraded 数 → " + (($c.degraded | Measure-Object).Count))
}
$rt = Join-Path $data "runtime.json"
if (Test-Path $rt) {
    $r = Get-Content $rt -Raw -Encoding UTF8 | ConvertFrom-Json
    Say ("  runtime.services 数 → " + (($r.services | Measure-Object).Count))
}

Say "== 4.2 入口（从 VM 本机访问）=="
foreach ($u in @("http://127.0.0.1:5210/api/health","http://127.0.0.1:5700/","http://127.0.0.1:8080/login.html","http://127.0.0.1:5102/assets/")) {
    try {
        $resp = Invoke-WebRequest -Uri $u -TimeoutSec 5 -UseBasicParsing
        Say ("  " + $u + " → " + [int]$resp.StatusCode)
    } catch {
        $code = try { [int]$_.Exception.Response.StatusCode.value__ } catch { 0 }
        Say ("  " + $u + " → " + $(if ($code) { $code } else { "连接失败" }))
    }
}

Say "== 4.6 提示词占位符已渲染 =="
$gp = Join-Path $data "config\orchestrator\system_prompt\GLOBAL_SETTING.md"
if (Test-Path $gp) {
    $has = (Get-Content $gp -Raw -Encoding UTF8) -match '\{\{WORKSPACE_DIR\}\}'
    Say ("  模板里仍是占位符（预期 true）→ " + $has)
}

Say "== 4.7 技能 CLI =="
$claw = Join-Path $env:APPDATA "npm\clawhub.cmd"
Say ("  clawhub.cmd 存在 → " + (Test-Path $claw))
if (Test-Path $claw) { Say ("  clawhub --version → " + ((& $claw --version 2>&1) -join " ")) }

Say "== 4.8 QQ 适配器 =="
$log = Join-Path $data "logs\qq-onebot-service.log"
if (Test-Path $log) {
    $tail = Get-Content $log -Tail 30 -Encoding UTF8
    Say ("  最近出现 QQ 已上线 → " + [bool]($tail | Select-String -Pattern 'QQ 已上线'))
}

$txt = ($lines -join "`n")
$body = @{ stage = "doc-walk"; host = "vm"; ok = $true; detail = $txt } | ConvertTo-Json -Depth 3
try {
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($body)
    Invoke-WebRequest -Uri "http://10.244.37.37:8000/report" -Method Post -Body $bytes -ContentType "application/json; charset=utf-8" -TimeoutSec 20 -UseBasicParsing | Out-Null
    Write-Host "已回报"
} catch { Write-Host ("回报失败: " + $_.Exception.Message) }

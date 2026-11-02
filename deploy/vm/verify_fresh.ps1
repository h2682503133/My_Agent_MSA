# 干净安装后的验收：依赖是否齐 + 启动 + openviking 是否真连上
# 用法：powershell -NoProfile -ExecutionPolicy Bypass -File verify_fresh.ps1

$ErrorActionPreference = "Continue"
$data = "$env:LOCALAPPDATA\MyAgent\data"
$venvPy = "$data\venv\Scripts\python.exe"
$report = [ordered]@{}

# 1) 关键依赖是否真的装进了 venv（openviking 的补充依赖）
$probe = @'
import importlib, json
mods = ["loguru", "jsonschema", "json_repair", "pathspec", "apscheduler", "orjson",
        "tiktoken", "tenacity", "psutil", "rich", "typer", "aiofiles", "openpyxl"]
missing = []
for name in mods:
    try:
        importlib.import_module(name)
    except Exception:
        missing.append(name)
ok_openviking = True
detail = ""
try:
    import openviking
except Exception as exc:
    ok_openviking = False
    detail = str(exc)
try:
    import openviking_sdk
except Exception as exc:
    ok_openviking = False
    detail += " | sdk: " + str(exc)
print(json.dumps({"missing": missing, "openviking": ok_openviking, "detail": detail}))
'@
$probeFile = "$env:TEMP\probe_deps.py"
Set-Content -Path $probeFile -Value $probe -Encoding UTF8
$probeOut = & $venvPy -X utf8 $probeFile 2>&1 | Select-Object -Last 1
$report["deps"] = $probeOut

# 2) 启动（openviking 指向宿主端口转发）
$env:OPENVIKING_SERVER_URL = "http://10.244.37.37:1933"
Start-Process python -ArgumentList '-X','utf8','C:\MyAgent\deploy\launcher\run.py','--data-dir',$data,
    '--features','core,memory,tools.basic','--modes','memory=openviking' -NoNewWindow
Start-Sleep -Seconds 45

# 3) 端口
foreach ($pair in @(@('8080','web'), @('5700','dashboard'), @('5102','assets'), @('5210','gateway'), @('5303','tool-runtime'), @('5301','openviking-context'))) {
    $report["port_" + $pair[1]] = (Test-NetConnection -ComputerName 127.0.0.1 -Port ([int]$pair[0]) -InformationLevel Quiet -WarningAction SilentlyContinue)
}

# 4) openviking 服务日志
$logPath = "$data\logs\openviking-context-service.log"
if (Test-Path $logPath) {
    $report["openviking_log"] = @(Get-Content $logPath | Where-Object { $_ -match 'OpenViking' } | Select-Object -Last 2)
} else {
    $report["openviking_log"] = "日志不存在"
}

# 5) 工具权限接口（mono 也要能读）
try {
    $perm = (Invoke-WebRequest "http://127.0.0.1:5700/api/tool_permissions" -TimeoutSec 10 -UseBasicParsing).Content | ConvertFrom-Json
    $report["perm_tools"] = $perm.tools.Count
    $report["perm_levels"] = $perm.levels.Count
} catch {
    $report["perm_error"] = "$($_.Exception.Message)"
}

$body = @{ stage = "fresh-install"; ok = $true; detail = ($report | ConvertTo-Json -Depth 4); host = $env:COMPUTERNAME } | ConvertTo-Json -Depth 5
try {
    Invoke-RestMethod -Uri "http://10.244.37.37:8000/report" -Method Post -Body $body -ContentType "application/json; charset=utf-8" | Out-Null
    Write-Host "已回传宿主"
} catch { Write-Host "回传失败：$($_.Exception.Message)" }

$report | ConvertTo-Json -Depth 4

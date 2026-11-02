param(
    [string]$HostUrl = "http://10.244.37.37:8000",
    [string]$SatoriHost = "10.244.37.37",
    [int]$SatoriPort = 5600,
    [string]$SatoriToken = "Hzh123654789",
    [switch]$Start
)
$ErrorActionPreference = "Stop"
$dataDir = Join-Path $env:LOCALAPPDATA "MyAgent\data"
$cfgDir = Join-Path $dataDir "config\qq-llbot"
New-Item -ItemType Directory -Force -Path $cfgDir | Out-Null
$cfgPath = Join-Path $cfgDir "qq_llbot_config.json"

$cfg = [ordered]@{
    scheduler_target   = "127.0.0.1:5100"
    subscriber_id      = "qq-llbot-1"
    subscriber_priority = 100
    group_at_required  = "true"
    satori             = [ordered]@{ host = $SatoriHost; port = $SatoriPort; token = $SatoriToken }
}
$cfg | ConvertTo-Json -Depth 5 | Set-Content $cfgPath -Encoding UTF8
Write-Host ("qq_llbot_config.json 已写入: " + $cfgPath)
Get-Content $cfgPath | Write-Host

if ($Start) {
    $python = Join-Path $dataDir "venv\Scripts\python.exe"
    $runPy = "C:\MyAgent\deploy\launcher\run.py"
    Write-Host "== stop mono =="
    & $python -X utf8 $runPy --data-dir $dataDir --stop
    Start-Sleep -Seconds 3
    Write-Host "== start mono (qq=satori) =="
    & $python -X utf8 $runPy --data-dir $dataDir --features "core,memory,tools.basic,skills,qq" --modes "memory=local,qq=satori"
}

param(
    [string]$LogName = "qq-onebot-service",
    [int]$Tail = 40,
    [string]$HostUrl = "http://10.244.37.37:8000"
)
$ErrorActionPreference = "Stop"
$dataDir = Join-Path $env:LOCALAPPDATA "MyAgent\data"
$logPath = Join-Path $dataDir ("logs\" + $LogName + ".log")
$body = [ordered]@{
    stage = "vm-mono-onebot"
    log   = $LogName
    path  = $logPath
    exists = (Test-Path $logPath)
    lines = @()
}
if (Test-Path $logPath) {
    $body.lines = @(Get-Content $logPath -Tail $Tail -Encoding UTF8)
}
$json = $body | ConvertTo-Json -Depth 4
try {
    $resp = Invoke-RestMethod -Uri ($HostUrl + "/report") -Method Post -ContentType "application/json; charset=utf-8" -Body ([System.Text.Encoding]::UTF8.GetBytes($json))
    Write-Host ("report sent: " + ($resp | ConvertTo-Json -Compress))
} catch {
    Write-Host ("report failed: " + $_.Exception.Message)
}
# 顺带把关键行直接打印出来，方便截图
if (Test-Path $logPath) { Get-Content $logPath -Tail 15 -Encoding UTF8 }

param(
    [string]$Features = "core,memory,tools.basic,skills,qq,timer",
    [string]$Modes = "memory=openviking,memory.source=install,qq=onebot"
)
$ErrorActionPreference = "Continue"
$dataDir = Join-Path $env:LOCALAPPDATA "MyAgent\data"
$py = Join-Path $dataDir "venv\Scripts\python.exe"
$runPy = "C:\MyAgent\deploy\launcher\run.py"

Write-Host "== stop mono =="
& $py -X utf8 $runPy --data-dir $dataDir --stop
Start-Sleep -Seconds 3

$ws = Join-Path $dataDir "openviking\workspace"
if (Test-Path $ws) {
    Write-Host ("== 清空旧向量库（维度变了）: " + $ws)
    Remove-Item $ws -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Host "== start mono =="
& $py -X utf8 $runPy --data-dir $dataDir --features $Features --modes $Modes

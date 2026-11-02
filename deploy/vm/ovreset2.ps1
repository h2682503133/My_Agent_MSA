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
Start-Sleep -Seconds 2

Write-Host "== 杀掉残留 openviking-server 进程（防止端口被旧进程占着）"
Get-Process openviking-server -ErrorAction SilentlyContinue | ForEach-Object {
    Write-Host ("  kill pid " + $_.Id)
    Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
}
Start-Sleep -Seconds 3
$still = (Get-NetTCPConnection -LocalPort 1933 -State Listen -ErrorAction SilentlyContinue | Measure-Object).Count
Write-Host ("  1933 监听数: " + $still)

Write-Host "== 清空旧向量库（embedding 维度已变）"
$ws = Join-Path $dataDir "openviking\workspace"
if (Test-Path $ws) { Remove-Item $ws -Recurse -Force -ErrorAction SilentlyContinue }

Write-Host "== start mono =="
& $py -X utf8 $runPy --data-dir $dataDir --features $Features --modes $Modes

$dataDir = Join-Path $env:LOCALAPPDATA "MyAgent\data"
$py = Join-Path $dataDir "venv\Scripts\python.exe"
$repo = "C:\MyAgent"
$sc = Join-Path $dataDir "venvs\openviking\Lib\site-packages\sitecustomize.py"
Write-Host "== 清掉调试用 sitecustomize =="
if (Test-Path $sc) { Remove-Item $sc -Force; Write-Host "   已删除" } else { Write-Host "   本来就没有" }

Write-Host "== 验证启动锁：再起一个启动器应被拒绝 =="
& $py -X utf8 "$repo\deploy\launcher\run.py" --data-dir $dataDir --features core,memory,tools.basic,qq --modes memory=local,qq=onebot --no-deps 2>&1 | Select-Object -First 3 | ForEach-Object { Write-Host ("   " + $_) }

Write-Host "== 验证收尾：先看 pids.json 数量 =="
$p = Join-Path $dataDir "pids.json"
if (Test-Path $p) {
    $j = Get-Content $p -Raw | ConvertFrom-Json
    Write-Host ("   当前记录进程数: " + ($j.processes | Measure-Object).Count)
}

param(
    [string]$Features = "core,memory,tools.basic,skills,qq,timer",
    [string]$Modes = "memory=openviking,memory.source=install,qq=onebot"
)
$ErrorActionPreference = "Continue"
$dataDir = Join-Path $env:LOCALAPPDATA "MyAgent\data"
$py = Join-Path $dataDir "venv\Scripts\python.exe"
$repo = "C:\MyAgent"

Write-Host "== 拉最新仓库 =="
iwr "http://10.244.37.37:8000/repo.zip" -OutFile $env:TEMP\r.zip -UseBasicParsing
Expand-Archive $env:TEMP\r.zip $repo -Force

Write-Host "== 停实例 + 清残留 =="
& $py -X utf8 "$repo\deploy\launcher\run.py" --data-dir $dataDir --stop
Start-Sleep -Seconds 3
Get-Process python,openviking-server -ErrorAction SilentlyContinue | ForEach-Object {
    try { if ($_.Path -like "*MyAgent*" -or $_.Path -like "*venvs*") { Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue } } catch {}
}
Start-Sleep -Seconds 3

Write-Host "== 重写 hashlib 兼容补丁（--openviking 幂等）=="
& $py -X utf8 "$repo\deploy\launcher\deps.py" --data-dir $dataDir --features "core,memory" --openviking 2>&1 | Select-Object -Last 6

Write-Host "== 清空旧向量库并启动（本机自建 openviking）=="
Remove-Item (Join-Path $dataDir "openviking\workspace") -Recurse -Force -ErrorAction SilentlyContinue
Start-Process -FilePath $py -ArgumentList '-X','utf8',"$repo\deploy\launcher\run.py",'--data-dir',$dataDir,'--features',$Features,'--modes',$Modes,'--no-deps' -WindowStyle Hidden -RedirectStandardOutput "$dataDir\ovretest.out" -RedirectStandardError "$dataDir\ovretest.err"
Start-Sleep -Seconds 90

Write-Host "== openviking 日志末尾 =="
Get-Content (Join-Path $dataDir "logs\openviking.log") -Tail 12 -ErrorAction SilentlyContinue | ForEach-Object { Write-Host ("   " + $_) }
Write-Host "== 启动输出末尾 =="
Get-Content (Join-Path $dataDir "ovretest.out") -Tail 10 -ErrorAction SilentlyContinue | ForEach-Object { Write-Host ("   " + $_) }

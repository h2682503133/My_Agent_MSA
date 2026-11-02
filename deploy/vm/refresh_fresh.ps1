param([string]$FreshDir = "C:\MyAgentFreshData", [string]$Features = "core,memory,tools.basic")
$ErrorActionPreference = "Continue"
$repo = "C:\MyAgent"
$basePython = "C:\Users\vboxuser\AppData\Local\Programs\Python\Python311\python.exe"

Write-Host "== 拉最新仓库 =="
iwr "http://10.244.37.37:8000/repo.zip" -OutFile $env:TEMP\r.zip -UseBasicParsing
Expand-Archive $env:TEMP\r.zip $repo -Force

Write-Host "== 停掉 fresh 实例 =="
& $basePython -X utf8 "$repo\deploy\launcher\run.py" --data-dir $FreshDir --stop
Start-Sleep -Seconds 3
Get-Process python -ErrorAction SilentlyContinue | ForEach-Object {
    try { if ($_.Path -like "*MyAgent*" -or $_.Path -like "*venvs*") { Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue } } catch {}
}
Start-Sleep -Seconds 2

Write-Host "== 重新启动 fresh 实例 =="
Start-Process -FilePath $basePython -ArgumentList '-X','utf8',"$repo\deploy\launcher\run.py",'--data-dir',$FreshDir,'--features',$Features -WindowStyle Hidden -RedirectStandardOutput "$FreshDir\start2.out" -RedirectStandardError "$FreshDir\start2.err"
Start-Sleep -Seconds 50
if (Test-Path "$FreshDir\start2.out") { Get-Content "$FreshDir\start2.out" -Tail 12 | ForEach-Object { Write-Host ("   " + $_) } }

param(
    [string]$Features = "core,memory,tools.basic",
    [string]$FreshDir = "C:\MyAgentFreshData"
)
$ErrorActionPreference = "Continue"
$repo = "C:\MyAgent"
$basePython = "C:\Users\vboxuser\AppData\Local\Programs\Python\Python311\python.exe"
$log = New-Object System.Collections.Generic.List[string]
function Log($m) { $log.Add($m); Write-Host $m }

Log "== 0) 清场：杀掉所有遗留服务进程"
Get-Process python,openviking-server -ErrorAction SilentlyContinue | ForEach-Object {
    try { if ($_.Path -like "*MyAgent*" -or $_.Path -like "*venvs*") { Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue } } catch {}
}
Start-Sleep -Seconds 3
Log ("   遗留 python 进程数: " + (Get-Process python -ErrorAction SilentlyContinue | Measure-Object).Count)

Log "== 1) 拉最新仓库（模拟用户拿到新代码）"
iwr "http://10.244.37.37:8000/repo.zip" -OutFile $env:TEMP\fresh_repo.zip -UseBasicParsing
Expand-Archive $env:TEMP\fresh_repo.zip $repo -Force
Log "   仓库已更新"

Log "== 2) 全新数据目录"
if (Test-Path $FreshDir) { Remove-Item $FreshDir -Recurse -Force -ErrorAction SilentlyContinue }
Log ("   目录: " + $FreshDir + " 存在: " + (Test-Path $FreshDir))

Log "== 3) 照文档：唯一部署命令"
$sw = [Diagnostics.Stopwatch]::StartNew()
$out = & $basePython -X utf8 "$repo\deploy\launcher\deps.py" --data-dir $FreshDir --features $Features --install 2>&1
$code = $LASTEXITCODE
$sw.Stop()
$out | ForEach-Object { Log ("   " + $_) }
Log ("   部署耗时 " + [int]$sw.Elapsed.TotalSeconds + " 秒，退出码=" + $code)

Log "== 4) 部署产物检查（新用户视角）"
$checks = @("config\launcher.json", "config\orchestrator\system_prompt\GLOBAL_SETTING.md", "capability.json", "runtime.json", "venv\Scripts\python.exe", "deps\requirements.lock.txt")
foreach ($p in $checks) { Log ("   " + $p + " → " + (Test-Path (Join-Path $FreshDir $p))) }

Log "== 5) 照文档启动"
Start-Process -FilePath $basePython -ArgumentList '-X','utf8',"$repo\deploy\launcher\run.py",'--data-dir',$FreshDir,'--features',$Features -WindowStyle Hidden -RedirectStandardOutput "$FreshDir\start.out" -RedirectStandardError "$FreshDir\start.err"
Start-Sleep -Seconds 50
Log "== 6) 启动输出（末尾）"
if (Test-Path "$FreshDir\start.out") { Get-Content "$FreshDir\start.out" -Tail 18 | ForEach-Object { Log ("   " + $_) } }
if (Test-Path "$FreshDir\start.err") { Get-Content "$FreshDir\start.err" -Tail 6 | ForEach-Object { Log ("   ERR: " + $_) } }

$body = @{ stage = "fresh-deploy"; host = "vm"; ok = ($code -eq 0); detail = ($log -join "`n") } | ConvertTo-Json -Depth 3
$bytes = [System.Text.Encoding]::UTF8.GetBytes($body)
try {
    Invoke-WebRequest -Uri "http://10.244.37.37:8000/report" -Method Post -Body $bytes -ContentType "application/json; charset=utf-8" -TimeoutSec 20 -UseBasicParsing | Out-Null
    Write-Host "已回报宿主"
} catch { Write-Host ("回报失败: " + $_.Exception.Message) }

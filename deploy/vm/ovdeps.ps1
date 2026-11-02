$ErrorActionPreference = "Continue"
$log = New-Object System.Collections.Generic.List[string]
function Log($m) { $line = "[$(Get-Date -Format HH:mm:ss)] $m"; $log.Add($line); Write-Host $line }
function Post {
    try {
        $txt = ($log -join "`n")
        $body = @{ stage = "ov-deps"; host = "vm"; ok = $true; detail = $txt } | ConvertTo-Json -Depth 3
        Invoke-WebRequest -Uri "http://10.244.37.37:8000/report" -Method Post -Body $body -ContentType "application/json" -TimeoutSec 10 -UseBasicParsing | Out-Null
        Log "已回报宿主"
    } catch { Log ("回报失败: " + $_.Exception.Message) }
}

$py = "$env:TEMP\ovvenv\Scripts\python.exe"
$reqFile = "$env:TEMP\ovreqs.txt"
Log "生成依赖清单（剔除 volcengine 伞包）"
& $py -c "import importlib.metadata as m; print('\n'.join(r for r in (m.requires('openviking') or []) if 'volcengine' not in r.lower()))" | Set-Content $reqFile -Encoding UTF8
Log ("清单行数: " + (Get-Content $reqFile).Count)

Log "安装清单..."
$sw = [Diagnostics.Stopwatch]::StartNew()
& $py -m pip install --progress-bar off -r $reqFile 2>&1 | Tee-Object "$env:TEMP\pip_ov3.log" | Select-Object -Last 8 | ForEach-Object { Log "  $_" }
$sw.Stop()
Log ("安装完成，耗时 " + [int]$sw.Elapsed.TotalSeconds + " 秒")

Log "尝试 openviking-server --help"
$out = & "$env:TEMP\ovvenv\Scripts\openviking-server.exe" --help 2>&1
Log ("退出码: " + $LASTEXITCODE)
$out | Select-Object -First 15 | ForEach-Object { Log "  $_" }
Log "--- 末尾 ---"
$out | Select-Object -Last 12 | ForEach-Object { Log "  $_" }
Post

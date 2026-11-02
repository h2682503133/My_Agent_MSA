$ErrorActionPreference = "Continue"
$log = New-Object System.Collections.Generic.List[string]
function Log($m) { $line = "[$(Get-Date -Format HH:mm:ss)] $m"; $log.Add($line); Write-Host $line }
$py = "$env:TEMP\ovvenv\Scripts\python.exe"
$srv = "$env:TEMP\ovvenv\Scripts\openviking-server.exe"
$dir = "$env:TEMP\ovrun"
New-Item -ItemType Directory -Force -Path $dir | Out-Null
$ws = ($dir + "\workspace") -replace '\\','/'
$conf = @"
{
  "server": {"root_api_key": "test-root-key", "host": "127.0.0.1", "port": 1933, "workers": 1},
  "storage": {"workspace": "$ws"},
  "embedding": {
    "dense": {"provider": "openai", "api_base": "http://127.0.0.1:11434/v1", "api_key": "ollama", "model": "qwen3-embedding:0.6b", "dimension": 1024},
    "max_input_tokens": 24000
  },
  "vlm": {"provider": "volcengine", "api_base": "https://ark.cn-beijing.volces.com/api/v3", "api_key": "unused", "model": "unused", "timeout": 60}
}
"@
$confPath = "$dir\ov.conf"
$conf | Set-Content $confPath -Encoding UTF8
$env:OPENVIKING_CONFIG_FILE = $confPath

Log "启动 openviking-server ..."
$p = Start-Process -FilePath $srv -PassThru -RedirectStandardOutput "$dir\out.log" -RedirectStandardError "$dir\err.log" -WindowStyle Hidden
Log "pid=$($p.Id)"

$ok = $false
for ($i = 0; $i -lt 15; $i++) {
    Start-Sleep -Seconds 3
    if ($p.HasExited) { Log "进程已退出，code=$($p.ExitCode)"; break }
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:1933/health" -TimeoutSec 3 -UseBasicParsing
        $body = ($r.Content -replace "`r?`n", " ")
        Log ("/health → " + [int]$r.StatusCode + " " + $body.Substring(0, [Math]::Min(200, $body.Length)))
        $ok = $true
        break
    } catch {
        if ($i -eq 14) { Log ("/health 最终失败: " + $_.Exception.Message) }
    }
}
Log ("结论: " + $(if ($ok) { "SERVER OK" } else { "SERVER FAIL" }))

Log "--- err.log 末尾 ---"
if (Test-Path "$dir\err.log") { Get-Content "$dir\err.log" -Tail 15 | ForEach-Object { Log "  $_" } }
Log "--- out.log 末尾 ---"
if (Test-Path "$dir\out.log") { Get-Content "$dir\out.log" -Tail 15 | ForEach-Object { Log "  $_" } }

if (-not $p.HasExited) { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue; Log "已停止测试服务" }

try {
    $txt = ($log -join "`n")
    $body = @{ stage = "ov-run"; host = "vm"; ok = $ok; detail = $txt } | ConvertTo-Json -Depth 3
    Invoke-WebRequest -Uri "http://10.244.37.37:8000/report" -Method Post -Body $body -ContentType "application/json" -TimeoutSec 10 -UseBasicParsing | Out-Null
    Log "已回报宿主"
} catch { Log ("回报失败: " + $_.Exception.Message) }

param(
    [string]$ReportUrl = "http://10.244.37.37:8000/report",
    [string]$VenvDir = "$env:TEMP\ovvenv",
    [int]$Port = 1933
)
$ErrorActionPreference = "Continue"
$log = New-Object System.Collections.Generic.List[string]
function Log($m) { $line = "[$(Get-Date -Format HH:mm:ss)] $m"; $log.Add($line); Write-Host $line }

try {
    $base = (Get-Command python).Source
    Log "base python: $base"
    Log (& $base -V 2>&1)

    if (-not (Test-Path "$VenvDir\Scripts\python.exe")) {
        Log "创建隔离 venv: $VenvDir"
        & $base -m venv $VenvDir 2>&1 | ForEach-Object { Log "  $_" }
    } else { Log "venv 已存在" }
    $py = "$VenvDir\Scripts\python.exe"

    Log "升级 pip..."
    & $py -m pip install --upgrade pip -q 2>&1 | Select-Object -Last 3 | ForEach-Object { Log "  $_" }

    Log "开始安装 openviking（完整依赖，可能几分钟）..."
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $out = & $py -m pip install openviking 2>&1
    $sw.Stop()
    $out | Select-Object -Last 25 | ForEach-Object { Log "  $_" }
    Log ("安装耗时: " + [int]$sw.Elapsed.TotalSeconds + " 秒, 退出码=$LASTEXITCODE")

    Log "检查入口:"
    $exe = "$VenvDir\Scripts\openviking-server.exe"
    Log ("  openviking-server.exe 存在: " + (Test-Path $exe))
    & $py -c "import openviking, sys; print('  openviking', getattr(openviking,'__version__','?'), sys.version.split()[0])" 2>&1 | ForEach-Object { Log $_ }

    # 生成测试 ov.conf
    $confDir = "$env:TEMP\ovtest"
    New-Item -ItemType Directory -Force -Path $confDir | Out-Null
    $ws = ($confDir + "\workspace") -replace '\\','/'
    $conf = @"
{
  "server": {"root_api_key": "test-root-key", "host": "127.0.0.1", "port": $Port, "workers": 1},
  "storage": {"workspace": "$ws"},
  "embedding": {
    "dense": {"provider": "openai", "api_base": "http://127.0.0.1:11434/v1", "api_key": "ollama", "model": "qwen3-embedding:0.6b", "dimension": 1024},
    "max_input_tokens": 24000
  },
  "vlm": {"provider": "volcengine", "api_base": "https://ark.cn-beijing.volces.com/api/v3", "api_key": "unused", "model": "unused", "timeout": 60}
}
"@
    $confPath = "$confDir\ov.conf"
    $conf | Set-Content $confPath -Encoding UTF8
    Log "ov.conf: $confPath"

    Log "启动 openviking-server（后台）..."
    $env:OPENVIKING_CONFIG_FILE = $confPath
    $proc = Start-Process -FilePath $exe -PassThru -RedirectStandardOutput "$confDir\srv.out" -RedirectStandardError "$confDir\srv.err" -WindowStyle Hidden
    Log "  pid=$($proc.Id)"

    $ok = $false
    for ($i = 0; $i -lt 20; $i++) {
        Start-Sleep -Seconds 3
        try {
            $r = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 3 -UseBasicParsing
            Log ("  /health → " + [int]$r.StatusCode + "  " + ($r.Content -replace "`r?`n"," ").Substring(0,[Math]::Min(200,$r.Content.Length)))
            $ok = $true
            break
        } catch {
            if ($i -eq 19) { Log ("  最后一次 /health 失败: " + $_.Exception.Message) }
        }
    }
    Log ("健康检查结果: " + $(if ($ok) { "PASS" } else { "FAIL" }))

    Log "--- srv.err 末尾 ---"
    if (Test-Path "$confDir\srv.err") { Get-Content "$confDir\srv.err" -Tail 20 | ForEach-Object { Log "  $_" } }
    Log "--- srv.out 末尾 ---"
    if (Test-Path "$confDir\srv.out") { Get-Content "$confDir\srv.out" -Tail 20 | ForEach-Object { Log "  $_" } }

    if ($ok) {
        try {
            $body = '{"root_api_key":"test-root-key"}'
            $r2 = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/v1/meta" -Method Post -Body $body -ContentType "application/json" -TimeoutSec 5 -UseBasicParsing
            Log ("  POST /v1/meta → " + [int]$r2.StatusCode + " " + $r2.Content.Substring(0,[Math]::Min(200,$r2.Content.Length)))
        } catch { Log ("  POST /v1/meta 失败: " + $_.Exception.Message) }
    }

    if (-not $proc.HasExited) { Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue; Log "已停止测试服务" }

    $txt = ($log -join "`n")
    try {
        $hash = @{ text = $txt.Substring(0, [Math]::Min(6000, $txt.Length)) } | ConvertTo-Json
        Invoke-WebRequest -Uri $ReportUrl -Method Post -Body $hash -ContentType "application/json" -TimeoutSec 10 -UseBasicParsing | Out-Null
        Log "已回报宿主"
    } catch { Log ("回报失败: " + $_.Exception.Message) }
} catch {
    Log ("异常: " + $_.Exception.Message)
    Log ($_.ScriptStackTrace)
    try {
        $txt = ($log -join "`n")
        $hash = @{ text = $txt } | ConvertTo-Json
        Invoke-WebRequest -Uri $ReportUrl -Method Post -Body $hash -ContentType "application/json" -TimeoutSec 10 -UseBasicParsing | Out-Null
    } catch {}
}

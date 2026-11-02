$ErrorActionPreference = "Continue"
$dataDir = Join-Path $env:LOCALAPPDATA "MyAgent\data"
$ov = Join-Path $dataDir "venvs\openviking"
$py = Join-Path $ov "Scripts\python.exe"
$srv = Join-Path $ov "Scripts\openviking-server.exe"

Write-Host "== 停服务 =="
& (Join-Path $dataDir "venv\Scripts\python.exe") -X utf8 "C:\MyAgent\deploy\launcher\run.py" --data-dir $dataDir --stop 2>&1 | Out-Null
Start-Sleep -Seconds 3
Get-Process python,openviking-server -ErrorAction SilentlyContinue | ForEach-Object {
    try { if ($_.Path -like "*MyAgent*") { Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue } } catch {}
}
Start-Sleep -Seconds 2

Write-Host "== 装调试补丁：error 时打印完整堆栈 =="
$sp = (& $py -c "import site;print(site.getsitepackages()[0])").Trim()
$patch = @"
import logging, traceback
_orig = logging.Logger.error
def _error(self, msg, *a, **k):
    try:
        if isinstance(msg, str) and 'Error reading existing record' in msg:
            traceback.print_exc()
    except Exception:
        pass
    return _orig(self, msg, *a, **k)
logging.Logger.error = _error
"@
Set-Content -Path (Join-Path $sp "sitecustomize.py") -Value $patch -Encoding UTF8

Write-Host "== 直接启动 openviking-server（带调试）=="
$env:OPENVIKING_CONFIG_FILE = Join-Path $dataDir "config\openviking\ov.conf"
$p = Start-Process -FilePath $srv -PassThru -RedirectStandardOutput "$env:TEMP\ovd.out" -RedirectStandardError "$env:TEMP\ovd.err" -WindowStyle Hidden
Start-Sleep -Seconds 35
Write-Host "== 触发一次写入（用 init 脚本访问接口不算写；这里直接 curl 一次 append 由上下文服务做，先用健康检查确认起来）=="
try { $h = Invoke-WebRequest -Uri "http://127.0.0.1:1933/health" -TimeoutSec 5 -UseBasicParsing; Write-Host ("  health: " + $h.Content.Substring(0,[Math]::Min(120,$h.Content.Length))) } catch { Write-Host "  health 失败" }
Start-Sleep -Seconds 10
Write-Host "== err 里的 traceback（取最后 40 行）=="
Get-Content "$env:TEMP\ovd.err" -Tail 40 -ErrorAction SilentlyContinue | ForEach-Object { Write-Host ("   " + $_) }
if (-not $p.HasExited) { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue }

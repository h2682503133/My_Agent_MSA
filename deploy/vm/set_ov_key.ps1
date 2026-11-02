$key = 'bXktYWdlbnQ.YWdlbnQtc2VydmljZQ.ZmVhY2Q3NmU1NWYyN2JhNjA2YzgwMzIyNGQ5YzlhMGY0Mjg0ZTQ5ZDM4NDg2NjE3YTM2MjQzYzhlMWYxNWE2OQ'
$p = Join-Path $env:LOCALAPPDATA 'MyAgent\data\config\openviking\api_key'
New-Item -ItemType Directory -Force -Path (Split-Path $p) | Out-Null
Set-Content -Path $p -Value $key -Encoding ASCII
Write-Host ("api_key 已写入: " + $p + "  (长度 " + $key.Length + ")")

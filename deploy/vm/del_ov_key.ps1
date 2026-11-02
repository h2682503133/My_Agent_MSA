$p = Join-Path $env:LOCALAPPDATA "MyAgent\data\config\openviking\api_key"
if (Test-Path $p) { Remove-Item $p -Force; Write-Host ("已删除错误 api_key: " + $p) } else { Write-Host "api_key 不存在（无需删除）" }

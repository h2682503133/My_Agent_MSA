$data = Join-Path $env:LOCALAPPDATA "MyAgent\data"
$cap = Join-Path $data "capability.json"
Write-Host "== capability.degraded 明细 =="
if (Test-Path $cap) {
    $c = Get-Content $cap -Raw -Encoding UTF8 | ConvertFrom-Json
    Write-Host ("  runtime_tier: " + $c.runtime_tier)
    $i = 0
    foreach ($d in $c.degraded) {
        $i++
        Write-Host ("  [" + $i + "] " + ($d | ConvertTo-Json -Compress -Depth 4))
    }
    if ($i -eq 0) { Write-Host "  （无降级）" }
}
Write-Host "== 适配器日志最近连接 =="
$log = Join-Path $data "logs\qq-onebot-service.log"
if (Test-Path $log) {
    Get-Content $log -Tail 400 -Encoding UTF8 | Select-String -Pattern 'QQ 已上线|已连接|连接失败' | Select-Object -Last 3 | ForEach-Object { Write-Host ("  " + $_.Line.Trim()) }
}

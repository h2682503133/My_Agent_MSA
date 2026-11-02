$dataDir = Join-Path $env:LOCALAPPDATA "MyAgent\data"
$root = (Get-Content (Join-Path $dataDir "config\openviking\root_api_key") -Raw).Trim()
$base = "http://127.0.0.1:1933"

foreach ($id in @("myagent", "my_agent", "MyAgent", "my-agent")) {
    $body = '{"account_id":"' + $id + '"}'
    try {
        $r = Invoke-WebRequest -Uri ($base + "/api/v1/admin/accounts") -Method POST -Headers @{ "X-API-Key" = $root; "Content-Type" = "application/json" } -Body ([Text.Encoding]::UTF8.GetBytes($body)) -TimeoutSec 10 -UseBasicParsing
        Write-Host ("  " + $id + " → " + [int]$r.StatusCode + " " + $r.Content.Substring(0,[Math]::Min(180,$r.Content.Length)))
    } catch {
        $code = $_.Exception.Response.StatusCode.value__
        $txt = ""
        try { $sr = New-Object IO.StreamReader($_.Exception.Response.GetResponseStream()); $txt = $sr.ReadToEnd() } catch {}
        Write-Host ("  " + $id + " → HTTP " + $code + " " + $txt.Substring(0,[Math]::Min(180,$txt.Length)))
    }
}
Write-Host "== 现有账号 =="
$r2 = Invoke-WebRequest -Uri ($base + "/api/v1/admin/accounts") -Headers @{ "X-API-Key" = $root } -TimeoutSec 10 -UseBasicParsing
Write-Host ("  " + $r2.Content.Substring(0,[Math]::Min(300,$r2.Content.Length)))

$dataDir = Join-Path $env:LOCALAPPDATA "MyAgent\data"
$root = (Get-Content (Join-Path $dataDir "config\openviking\root_api_key") -Raw).Trim()
$base = "http://127.0.0.1:1933"
Write-Host ("root_key 长度: " + $root.Length)

function Call($method, $path, $body) {
    $h = @{ "X-API-Key" = $script:root; "Content-Type" = "application/json" }
    try {
        if ($body) {
            $r = Invoke-WebRequest -Uri ($base + $path) -Method $method -Headers $h -Body ([Text.Encoding]::UTF8.GetBytes($body)) -TimeoutSec 10 -UseBasicParsing
        } else {
            $r = Invoke-WebRequest -Uri ($base + $path) -Method $method -Headers $h -TimeoutSec 10 -UseBasicParsing
        }
        Write-Host ("  " + $method + " " + $path + " → " + [int]$r.StatusCode + " " + $r.Content.Substring(0,[Math]::Min(160,$r.Content.Length)))
    } catch {
        $code = $_.Exception.Response.StatusCode.value__
        $txt = ""
        try { $sr = New-Object IO.StreamReader($_.Exception.Response.GetResponseStream()); $txt = $sr.ReadToEnd() } catch {}
        Write-Host ("  " + $method + " " + $path + " → HTTP " + $code + " " + $txt.Substring(0,[Math]::Min(160,$txt.Length)))
    }
}

Write-Host "== GET accounts =="
Call "GET" "/api/v1/admin/accounts" $null
Write-Host "== POST accounts {account_id} =="
Call "POST" "/api/v1/admin/accounts" '{"account_id":"my-agent"}'
Write-Host "== POST accounts {account} =="
Call "POST" "/api/v1/admin/accounts" '{"account":"my-agent"}'
Write-Host "== GET accounts 再看 =="
Call "GET" "/api/v1/admin/accounts" $null
Write-Host "== POST users =="
Call "POST" "/api/v1/admin/accounts/my-agent/users" '{"user_id":"nh","role":"user"}'

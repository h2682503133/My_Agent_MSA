$ErrorActionPreference = "Continue"
$dataDir = Join-Path $env:LOCALAPPDATA "MyAgent\data"
$py = Join-Path $dataDir "venv\Scripts\python.exe"
Write-Host "== pull latest repo =="
iwr "http://10.244.37.37:8000/repo.zip" -OutFile $env:TEMP\repo3.zip -UseBasicParsing
Expand-Archive $env:TEMP\repo3.zip C:\MyAgent -Force
Write-Host "== deps.py --openviking =="
& $py -X utf8 C:\MyAgent\deploy\launcher\deps.py --data-dir $dataDir --features "core,memory" --openviking 2>&1 | Tee-Object "$env:TEMP\ovinstall.log"
$txt = (Get-Content "$env:TEMP\ovinstall.log" -Tail 25) -join "`n"
try {
    $body = @{ stage = "ov-install"; host = "vm"; ok = $true; detail = $txt } | ConvertTo-Json -Depth 3
    Invoke-WebRequest -Uri "http://10.244.37.37:8000/report" -Method Post -Body $body -ContentType "application/json" -TimeoutSec 10 -UseBasicParsing | Out-Null
} catch {}

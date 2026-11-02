$ErrorActionPreference = "Stop"
$data = Join-Path $env:LOCALAPPDATA "MyAgent\data"
$conf = Join-Path $data "config\openviking\ov.conf"
$json = Get-Content $conf -Raw -Encoding UTF8 | ConvertFrom-Json

if (-not $json.embedding) { $json | Add-Member -NotePropertyName embedding -NotePropertyValue ([pscustomobject]@{}) -Force }
$dense = [pscustomobject]@{
    provider  = "volcengine"
    api_base  = "https://ark.cn-beijing.volces.com/api/v3"
    api_key   = "6fca3fd5-5b84-4c05-a232-dd816910d632"
    model     = "ep-20260621064553-q2m69"
    dimension = 2048
}
if ($json.embedding.PSObject.Properties.Name -contains "dense") { $json.embedding.dense = $dense }
else { $json.embedding | Add-Member -NotePropertyName dense -NotePropertyValue $dense -Force }
if (-not ($json.embedding.PSObject.Properties.Name -contains "max_input_tokens")) {
    $json.embedding | Add-Member -NotePropertyName max_input_tokens -NotePropertyValue 24000 -Force
}

$json | ConvertTo-Json -Depth 12 | Set-Content $conf -Encoding UTF8
Write-Host "ov.conf 已更新：embedding → volcengine / ep-20260621064553-q2m69 / 2048"
Get-Content $conf | Select-Object -First 24

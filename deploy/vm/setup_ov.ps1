param(
    [string]$ServerUrl = "http://10.244.37.37:1933",
    [string]$EmbeddingTarget = "",
    [switch]$Start
)
$ErrorActionPreference = "Stop"
$dataDir = Join-Path $env:LOCALAPPDATA "MyAgent\data"
$cfgPath = Join-Path $dataDir "config\launcher.json"
$raw = Get-Content $cfgPath -Raw -Encoding UTF8
$cfg = $raw | ConvertFrom-Json

function Set-Prop($obj, $name, $value) {
    if ($obj.PSObject.Properties.Name -contains $name) { $obj.$name = $value }
    else { $obj | Add-Member -NotePropertyName $name -NotePropertyValue $value -Force }
}

if (-not $cfg.openviking) {
    Set-Prop $cfg "openviking" ([pscustomobject]@{})
}
Set-Prop $cfg.openviking "server_url" $ServerUrl
if ($EmbeddingTarget) { Set-Prop $cfg.openviking "embedding_target" $EmbeddingTarget }
$cfg | ConvertTo-Json -Depth 12 | Set-Content $cfgPath -Encoding UTF8
Write-Host ("launcher.json 已更新: openviking.server_url = " + $ServerUrl + " | embedding_target = " + $EmbeddingTarget)

if ($Start) {
    $python = Join-Path $dataDir "venv\Scripts\python.exe"
    $runPy = "C:\MyAgent\deploy\launcher\run.py"
    Write-Host "== stop mono =="
    & $python -X utf8 $runPy --data-dir $dataDir --stop
    Start-Sleep -Seconds 3
    Write-Host "== start mono (memory=openviking + timer) =="
    & $python -X utf8 $runPy --data-dir $dataDir --features "core,memory,tools.basic,skills,qq,timer" --modes "memory=openviking,qq=onebot"
}

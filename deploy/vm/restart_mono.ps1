param(
    [string]$HostUrl = "http://10.244.37.37:8000",
    [string]$Features = "core,memory,tools.basic,skills,qq",
    [string]$Modes = "memory=local,qq=onebot"
)
$ErrorActionPreference = "Stop"
$dataDir = Join-Path $env:LOCALAPPDATA "MyAgent\data"
$python = Join-Path $dataDir "venv\Scripts\python.exe"
$runPy = "C:\MyAgent\deploy\launcher\run.py"

Write-Host "== stop mono =="
& $python -X utf8 $runPy --data-dir $dataDir --stop
Start-Sleep -Seconds 3

Write-Host "== pull latest repo =="
iwr ($HostUrl + "/repo.zip") -OutFile $env:TEMP\repo2.zip -UseBasicParsing
Expand-Archive $env:TEMP\repo2.zip C:\MyAgent -Force

Write-Host "== start mono (qq=onebot) =="
& $python -X utf8 $runPy --data-dir $dataDir --features $Features --modes $Modes

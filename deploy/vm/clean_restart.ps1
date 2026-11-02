param(
    [string]$Features = "core,memory,tools.basic,skills,qq,timer",
    [string]$Modes = "memory=local,qq=onebot"
)
$ErrorActionPreference = "Continue"
$dataDir = Join-Path $env:LOCALAPPDATA "MyAgent\data"
$py = Join-Path $dataDir "venv\Scripts\python.exe"
$runPy = "C:\MyAgent\deploy\launcher\run.py"

Write-Host "== stop mono =="
& $py -X utf8 $runPy --data-dir $dataDir --stop
Start-Sleep -Seconds 3

Write-Host "== 清理孤儿进程（只清本机单体相关）"
foreach ($name in @("python", "openviking-server")) {
    Get-Process $name -ErrorAction SilentlyContinue | ForEach-Object {
        $path = ""
        try { $path = $_.Path } catch { }
        if ($path -like "*MyAgent*" -or $path -like "*venvs*") {
            Write-Host ("  kill " + $name + " pid=" + $_.Id)
            Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
        }
    }
}
Start-Sleep -Seconds 2

Write-Host "== start mono =="
& $py -X utf8 $runPy --data-dir $dataDir --features $Features --modes $Modes

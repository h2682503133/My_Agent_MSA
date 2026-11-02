param(
    [string]$Features = "core,memory,tools.basic,skills,qq,timer",
    [string]$Modes = "memory=openviking,memory.source=install,qq=onebot"
)
$ErrorActionPreference = "Continue"
$dataDir = Join-Path $env:LOCALAPPDATA "MyAgent\data"
$py = Join-Path $dataDir "venv\Scripts\python.exe"
$repo = "C:\MyAgent"

Write-Host "== 拉仓库 =="
iwr "http://10.244.37.37:8000/repo.zip" -OutFile $env:TEMP\r.zip -UseBasicParsing
Expand-Archive $env:TEMP\r.zip $repo -Force

Write-Host "== 停 + 清残留 =="
& $py -X utf8 "$repo\deploy\launcher\run.py" --data-dir $dataDir --stop
Start-Sleep -Seconds 3
Get-Process python,openviking-server -ErrorAction SilentlyContinue | ForEach-Object {
    try { if ($_.Path -like "*MyAgent*") { Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue } } catch {}
}
Start-Sleep -Seconds 3

Write-Host "== 重装 hashlib 补丁 =="
& $py -X utf8 "$repo\deploy\launcher\deps.py" --data-dir $dataDir --features "core,memory" --openviking 2>&1 | Select-Object -Last 3

Write-Host "== 清库 + 删 key（模拟干净首次）=="
Remove-Item (Join-Path $dataDir "openviking\workspace") -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item (Join-Path $dataDir "config\openviking\api_key") -Force -ErrorAction SilentlyContinue

Write-Host "== 启动（会自动建 account + 初始化）=="
Start-Process -FilePath $py -ArgumentList '-X','utf8',"$repo\deploy\launcher\run.py",'--data-dir',$dataDir,'--features',$Features,'--modes',$Modes,'--no-deps' -WindowStyle Hidden -RedirectStandardOutput "$dataDir\ovf2.out" -RedirectStandardError "$dataDir\ovf2.err"
Start-Sleep -Seconds 130

Write-Host "== 初始化结果 =="
Select-String -Path "$dataDir\ovf2.out" -Pattern 'openviking 初始化|未监听' -ErrorAction SilentlyContinue | Select-Object -Last 3 | ForEach-Object { Write-Host ("   " + $_.Line.Trim()) }
Write-Host "== api_key 是否生成 =="
$kf = Join-Path $dataDir "config\openviking\api_key"
Write-Host ("   " + (Test-Path $kf))

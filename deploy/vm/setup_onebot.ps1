# 在虚拟机里执行：把单体 launcher.json 配成"连宿主 NapCat"的 OneBot 模式。
# 由宿主 serve.py 提供下载（/setup_onebot.ps1），避免在 VM 里手打长命令。
param(
    [string]$WsUrl = "ws://10.244.37.37:3001",
    # 真实 token 不要写进仓库：用 -Token 传入，或先设置环境变量 NAPCAT_TOKEN
    [string]$Token = $env:NAPCAT_TOKEN,
    [string]$SelfId = "2682503133",
    [string]$AssetsBase = "http://10.244.62.117:5102",
    [switch]$Start
)

if (-not $Token) {
    Write-Host "缺少 Token：请用 -Token <NapCat OneBot WS Token> 或先设置环境变量 NAPCAT_TOKEN"
    exit 2
}

$ErrorActionPreference = "Stop"
$dataDir = Join-Path $env:LOCALAPPDATA "MyAgent\data"
$cfgPath = Join-Path $dataDir "config\launcher.json"
if (-not (Test-Path $cfgPath)) { throw "no launcher.json: $cfgPath" }

$raw = Get-Content $cfgPath -Raw -Encoding UTF8
$cfg = $raw | ConvertFrom-Json

function Set-Prop($obj, [string]$name, $value) {
    if ($null -eq $obj.PSObject.Properties[$name]) {
        $obj | Add-Member -NotePropertyName $name -NotePropertyValue $value -Force
    } else {
        $obj.PSObject.Properties[$name].Value = $value
    }
}

if ($null -eq $cfg.PSObject.Properties["qq"]) {
    $cfg | Add-Member -NotePropertyName qq -NotePropertyValue ([pscustomobject]@{}) -Force
}
Set-Prop $cfg.qq "mode" "onebot"
Set-Prop $cfg.qq "onebot_mode" "forward"
Set-Prop $cfg.qq "ws_url" $WsUrl
Set-Prop $cfg.qq "ws_listen" "0.0.0.0:6700"
Set-Prop $cfg.qq "token" $Token
Set-Prop $cfg.qq "self_id" $SelfId
Set-Prop $cfg.qq "group_require_at" $true

if ($null -eq $cfg.PSObject.Properties["assets"]) {
    $cfg | Add-Member -NotePropertyName assets -NotePropertyValue ([pscustomobject]@{}) -Force
}
Set-Prop $cfg.assets "base" $AssetsBase

$cfg | ConvertTo-Json -Depth 8 | Set-Content $cfgPath -Encoding UTF8

Write-Host "launcher.json updated:"
Write-Host ("  qq.mode={0} onebot_mode={1}" -f $cfg.qq.mode, $cfg.qq.onebot_mode)
Write-Host ("  qq.ws_url={0}" -f $cfg.qq.ws_url)
Write-Host ("  qq.self_id={0} group_require_at={1}" -f $cfg.qq.self_id, $cfg.qq.group_require_at)
Write-Host ("  assets.base={0}" -f $cfg.assets.base)

if ($Start) {
    $python = Join-Path $dataDir "venv\Scripts\python.exe"
    if (-not (Test-Path $python)) { throw "no venv python: $python" }
    $runPy = "C:\MyAgent\deploy\launcher\run.py"
    Write-Host "starting mono (qq=onebot) ..."
    & $python -X utf8 $runPy --data-dir $dataDir --features "core,memory,tools.basic,skills,qq" --modes "memory=openviking,qq=onebot"
}

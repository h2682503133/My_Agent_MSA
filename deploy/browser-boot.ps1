# 宿主代理：从 admin panel 取「浏览器环境」命令并执行（启动 Edge 调试端口 + 中继 + 发布容器）。
#
# 为什么需要它：dashboard 跑在集群里，碰不到 Windows 宿主。所以由这个脚本常驻在宿主上，
# 轮询 dashboard 的 /api/browser/pending，执行完把结果回 /api/browser/report。
#
# 用法：
#   powershell -File deploy/browser-boot.ps1
#   powershell -File deploy/browser-boot.ps1 -Dashboard http://127.0.0.1:5700 -ProfileDir C:\edge-agent-profile
#
# 面板地址默认 http://127.0.0.1:5700（dashboard 已通过 lan-dashboard 容器发布到宿主 5700）。
param(
    [string]$Dashboard = 'http://127.0.0.1:5700',
    [string]$ProfileDir = 'C:\edge-agent-profile',
    [int]$DebugPort = 9222,
    [int]$RelayPort = 9223,
    [int]$PublishedPort = 9224,
    [string]$ContainerName = 'cdp-relay',
    [string]$SocatImage = 'hub.rat.dev/alpine/socat:latest',
    [string]$RelayScript = "$PSScriptRoot\browser-cdp-relay.ps1"
)

$ErrorActionPreference = 'Continue'
$EdgePath = 'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'
$RelayPidFile = Join-Path $env:TEMP 'cdp-relay.pid'

function Test-Port([int]$Port) {
    return [bool](Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue)
}

function Start-Edge {
    if (Test-Port $DebugPort) { return "已监听（未重复启动）" }
    if (-not (Test-Path $EdgePath)) { throw "找不到 Edge：$EdgePath" }
    New-Item -ItemType Directory -Force -Path $ProfileDir | Out-Null
    Start-Process -FilePath $EdgePath -ArgumentList `
        "--remote-debugging-port=$DebugPort", "--user-data-dir=$ProfileDir", 'about:blank' | Out-Null
    for ($i = 0; $i -lt 20; $i++) {
        Start-Sleep -Milliseconds 500
        if (Test-Port $DebugPort) { return "已启动（profile=$ProfileDir）" }
    }
    throw "Edge 启动了但 $DebugPort 没监听（独立 profile 路径是否可写？）"
}

function Start-Relay {
    if (Test-Port $RelayPort) { return "已监听" }
    if (-not (Test-Path $RelayScript)) { throw "找不到中继脚本：$RelayScript" }
    $proc = Start-Process powershell -ArgumentList '-NoProfile', '-WindowStyle', 'Hidden',
        '-ExecutionPolicy', 'Bypass', '-File', $RelayScript, '-Port', $RelayPort, '-TargetPort', $DebugPort `
        -PassThru -WindowStyle Hidden
    $proc.Id | Set-Content $RelayPidFile -Encoding UTF8
    for ($i = 0; $i -lt 20; $i++) {
        Start-Sleep -Milliseconds 400
        if (Test-Port $RelayPort) { return "已启动（pid=$($proc.Id)）" }
    }
    throw "中继脚本启动了但 $RelayPort 没监听"
}

function Ensure-Container {
    $exists = (docker ps -a --filter "name=^/$ContainerName$" --format '{{.Names}}') 2>$null
    if ($exists) {
        $running = (docker ps --filter "name=^/$ContainerName$" --format '{{.Names}}') 2>$null
        if ($running) { return "运行中" }
        docker start $ContainerName | Out-Null
        return "已重新启动"
    }
    docker run -d --name $ContainerName --restart unless-stopped -p "${PublishedPort}:${PublishedPort}" `
        $SocatImage "tcp-listen:$PublishedPort,fork,reuseaddr" "tcp-connect:host.docker.internal:$RelayPort" | Out-Null
    return "已创建并发布 $PublishedPort"
}

function Stop-Things {
    $notes = @()
    if (Test-Path $RelayPidFile) {
        $relayPid = Get-Content $RelayPidFile | Select-Object -First 1
        try {
            Stop-Process -Id ([int]$relayPid) -Force -ErrorAction Stop
            $notes += "已停止中继(pid=$relayPid)"
        } catch { $notes += "中继进程已不在" }
        Remove-Item $RelayPidFile -ErrorAction SilentlyContinue
    } else {
        # 没记到 PID（例如中继是手动起的）：按命令行兜底找一下
        $stale = Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandLine -match 'browser-cdp-relay' }
        if ($stale) {
            $stale | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
            $notes += "已停止中继进程 $($stale.Count) 个（无 PID 文件，按命令行匹配）"
        } else {
            $notes += "没有运行中的中继进程"
        }
    }
    $running = (docker ps --filter "name=^/$ContainerName$" --format '{{.Names}}') 2>$null
    if ($running) {
        docker stop $ContainerName | Out-Null
        $notes += "已停止容器 $ContainerName"
    }
    $notes += "Edge 未关闭（可能你正在用），需要时手动关"
    return ($notes -join '；')
}

function Invoke-Command2($command) {
    $action = $command.action
    $state = @{}
    $ok = $true
    $message = ''
    try {
        switch ($action) {
            'start' {
                $state.edge = Start-Edge
                $state.relay = Start-Relay
                $state.container = Ensure-Container
                $message = '浏览器环境已就绪'
            }
            'stop' {
                $message = Stop-Things
                $state.relay = 'stopped'
                $state.container = 'stopped'
            }
            'status' {
                $state.edge = if (Test-Port $DebugPort) { '监听中' } else { '未监听' }
                $state.relay = if (Test-Port $RelayPort) { '监听中' } else { '未监听' }
                $running = (docker ps --filter "name=^/$ContainerName$" --format '{{.Names}}') 2>$null
                $state.container = if ($running) { '运行中' } else { '未运行' }
                $message = '状态已采集'
            }
            default { throw "未知动作：$action" }
        }
    } catch {
        $ok = $false
        $message = "执行失败：$($_.Exception.Message)"
    }

    $payload = @{ id = [int]$command.id; ok = $ok; message = $message; state = $state } | ConvertTo-Json -Depth 4 -Compress
    try {
        # 必须按 UTF-8 字节发送，否则中文说明会变成 ???
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($payload)
        Invoke-RestMethod -Uri "$Dashboard/api/browser/report" -Method Post `
            -ContentType 'application/json; charset=utf-8' -Body $bytes -TimeoutSec 10 | Out-Null
    } catch {
        Write-Output "[boot] 上报失败：$($_.Exception.Message)"
    }
    Write-Output "[boot] #$($command.id) $action → ok=$ok $message"
}

Write-Output "[boot] 监听面板：$Dashboard（Ctrl+C 结束）"
while ($true) {
    try {
        $resp = Invoke-RestMethod -Uri "$Dashboard/api/browser/pending?wait=20" -TimeoutSec 30
        if ($resp -and $resp.command) {
            Invoke-Command2 $resp.command
        }
    } catch {
        Write-Output "[boot] 拉取命令失败：$($_.Exception.Message)"
        Start-Sleep -Seconds 5
    }
}

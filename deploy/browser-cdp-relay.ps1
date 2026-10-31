# Windows 宿主中继：把 Edge 的 CDP 端口（默认只绑 127.0.0.1）暴露给容器网络。
#
# 链路：
#   k8s pod（tool-runtime）
#     → 172.18.0.1:9224            （Docker 发布端口，由 cdp-relay 容器转发）
#     → host.docker.internal:9223   （本脚本监听 0.0.0.0:9223）
#     → 127.0.0.1:9222              （Edge 的 DevTools 端口）
#
# 用法：
#   powershell -File deploy/browser-cdp-relay.ps1              # 前台运行（Ctrl+C 停）
#   powershell -File deploy/browser-cdp-relay.ps1 -Port 9223 -TargetPort 9222
#
# 说明：
#   - Edge 必须已经带 `--remote-debugging-port=9222` 启动（见 deploy/browser-README.md）；
#   - Chromium 136+ 禁止对**默认 profile** 开调试端口，所以要用独立 user-data-dir；
#   - 本脚本只做 TCP 转发（字节流透传），不解析 CDP 内容。
param(
    [int]$Port = 9223,
    [int]$TargetPort = 9222,
    [string]$TargetHost = '127.0.0.1'
)

$ErrorActionPreference = 'Stop'

function Test-Upstream {
    try {
        $client = [System.Net.Sockets.TcpClient]::new()
        $client.Connect($TargetHost, $TargetPort)
        $client.Close()
        return $true
    } catch {
        return $false
    }
}

if (-not (Test-Upstream)) {
    Write-Output "[relay] 连不上 ${TargetHost}:$TargetPort —— 先按 deploy/browser-README.md 启动 Edge（带 --remote-debugging-port）"
    exit 1
}

$listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Any, $Port)
$listener.Start()
Write-Output "[relay] 监听 0.0.0.0:$Port → ${TargetHost}:$TargetPort（Ctrl+C 结束）"

while ($true) {
    $client = $listener.AcceptTcpClient()
    $upstream = $null
    try {
        $upstream = [System.Net.Sockets.TcpClient]::new()
        $upstream.Connect($TargetHost, $TargetPort)
    } catch {
        Write-Output "[relay] 上游连接失败：$($_.Exception.Message)"
        $client.Close()
        continue
    }

    $clientStream = $client.GetStream()
    $upstreamStream = $upstream.GetStream()
    $forward = $clientStream.CopyToAsync($upstreamStream)
    $backward = $upstreamStream.CopyToAsync($clientStream)
    try {
        [System.Threading.Tasks.Task]::WaitAny(@($forward, $backward)) | Out-Null
    } catch {
        # 连接被对端关闭属正常
    } finally {
        try { $client.Close() } catch { }
        try { $upstream.Close() } catch { }
    }
}

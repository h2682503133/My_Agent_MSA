# 局域网可访问的端口转发（kubectl port-forward 默认只 bind 127.0.0.1，局域网连不上）
#
# 用法：
#   powershell -File deploy/lan-forwards.ps1                 # 5210 / 3080 / 5700
#   powershell -File deploy/lan-forwards.ps1 -Ports 5210,3080,5700,80
#   powershell -File deploy/lan-forwards.ps1 -ShowAddress     # 只看本机局域网 IP
#
# 注意：
#   - 必须 `--address 0.0.0.0`，否则只监听回环地址；
#   - Windows 防火墙若拦，需要**管理员**执行加规则（脚本会提示命令，不自动改，避免误伤）。
param(
    [int[]]$Ports = @(5210, 3080, 5700),
    [string]$Namespace = 'agent',
    [string]$Service = 'gateway-backend-service',
    [switch]$ShowAddress,
    # 默认守护：port-forward 掉线（Pod 重建 / 网络抖动）后自动重连
    [switch]$NoSupervise,
    # 是否把虚拟网卡（WSL / VirtualBox / VPN）也算进"局域网地址"提示里
    [switch]$IncludeVirtual,
    [int]$RestartDelaySeconds = 3
)

$ErrorActionPreference = 'Stop'

# 计划任务/开机自启时 PATH 里可能没有 kubectl（Docker Desktop 的路径），显式兜底
if (-not (Get-Command kubectl -ErrorAction SilentlyContinue)) {
    $candidates = @(
        'C:\Program Files\Docker\Docker\resources\bin\kubectl.exe',
        "$env:ProgramFiles\Docker\Docker\resources\bin\kubectl.exe"
    )
    foreach ($candidate in $candidates) {
        if (Test-Path $candidate) {
            $env:Path = "$(Split-Path $candidate);$env:Path"
            break
        }
    }
    if (-not (Get-Command kubectl -ErrorAction SilentlyContinue)) {
        throw 'kubectl 不在 PATH，也找不到 Docker Desktop 自带的 kubectl.exe'
    }
}

function Get-LanAddresses {
    # 只取**网卡状态 Up 且拿到默认路由**的接口：断开的网卡会残留旧 IP（例如历史 10.x），
    # 拿它当访问地址必然连不上。虚拟网卡（WSL / VirtualBox / VPN）默认排除，可用 -IncludeVirtual 打开。
    $defaultIfIndexes = @(Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty InterfaceIndex)

    $adapters = Get-NetAdapter -ErrorAction SilentlyContinue | Where-Object { $_.Status -eq 'Up' }
    if (-not $IncludeVirtual) {
        $adapters = $adapters | Where-Object {
            $_.Name -notmatch 'vEthernet|VirtualBox|Radmin|VMware|Loopback|Tailscale|ZeroTier'
        }
    }

    foreach ($adapter in $adapters) {
        $isDefault = $defaultIfIndexes -contains $adapter.ifIndex
        Get-NetIPAddress -AddressFamily IPv4 -InterfaceIndex $adapter.ifIndex -ErrorAction SilentlyContinue |
            Where-Object { $_.AddressState -eq 'Preferred' -and $_.IPAddress -notmatch '^(127\.|169\.254\.)' } |
            Select-Object IPAddress, InterfaceAlias, @{Name = 'DefaultRoute'; Expression = { $isDefault } }
    }
}

if ($ShowAddress) {
    Write-Output '本机可用地址（DefaultRoute=True 的才是局域网设备该用的）：'
    Get-LanAddresses | Sort-Object -Property DefaultRoute -Descending | Format-Table -AutoSize
    exit 0
}

$mapping = ($Ports | ForEach-Object { "$_`:$($_)" }) -join ' '
Write-Output "转发（0.0.0.0）: $mapping  → svc/$Service -n $Namespace"
Write-Output ''
Write-Output '局域网访问地址（DefaultRoute=True 优先）：'
foreach ($addr in (Get-LanAddresses | Sort-Object -Property DefaultRoute -Descending)) {
    $tag = if ($addr.DefaultRoute) { '  ← 默认路由' } else { '' }
    foreach ($port in $Ports) {
        Write-Output ("  http://{0}:{1}/{2}" -f $addr.IPAddress, $port, $tag)
    }
}
Write-Output ''
Write-Output '如果其它设备仍连不上，多半是防火墙（需要管理员执行一次）：'
foreach ($port in $Ports) {
    Write-Output ("  netsh advfirewall firewall add rule name=`"my-agent-$port`" dir=in action=allow protocol=TCP localport=$port")
}
Write-Output ''

$forwardArgs = @($Ports | ForEach-Object { "$_`:$($_)" })

# port-forward 会因为 Pod 重建 / 网络抖动断掉（日志里是 "lost connection to pod"），
# 所以默认在外面套一层守护：断了就重连。
$attempt = 0
while ($true) {
    $attempt++
    if ($attempt -gt 1) {
        Write-Output ("[{0}] 第 {1} 次重连…" -f (Get-Date -Format 'HH:mm:ss'), $attempt)
    }
    kubectl -n $Namespace port-forward --address 0.0.0.0 "svc/$Service" @forwardArgs
    $code = $LASTEXITCODE
    Write-Output ("[{0}] port-forward 退出 code={1}" -f (Get-Date -Format 'HH:mm:ss'), $code)
    if ($NoSupervise) { exit $code }
    Start-Sleep -Seconds $RestartDelaySeconds
}

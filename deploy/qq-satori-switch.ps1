#!/usr/bin/env pwsh
# qq-llbot-service 的 satori 地址切换开关
#
#   cluster : 连集群内的 llbot（llbot.agent.svc.cluster.local:5600）
#   host    : 连宿主上的 LLBot（host.docker.internal:5600，旧路径/回滚用）
#
# 用法：
#   pwsh deploy/qq-satori-switch.ps1 -To cluster
#   pwsh deploy/qq-satori-switch.ps1 -To host
#   pwsh deploy/qq-satori-switch.ps1 -Status      # 只看当前指向
param(
    [ValidateSet('cluster', 'host', 'Status', 'status')]
    [string]$To = 'Status',
    [string]$Namespace = 'agent',
    [string]$Deployment = 'qq-llbot-service',
    [switch]$NoWait
)

$ErrorActionPreference = 'Stop'

$targets = @{
    cluster = 'llbot.agent.svc.cluster.local'
    host    = 'host.docker.internal'
}

function Get-CurrentHost {
    $json = kubectl -n $Namespace get deploy $Deployment -o json 2>$null
    if ($LASTEXITCODE -ne 0) { throw "读取 deployment $Deployment 失败" }
    $dep = $json | ConvertFrom-Json
    $envs = $dep.spec.template.spec.containers[0].env
    $item = $envs | Where-Object { $_.name -eq 'SATORI_HOST' }
    if ($null -eq $item) { return '(未设置，回退到配置文件)' }
    return $item.value
}

function Get-LlbotPodInfo {
    $pods = kubectl -n $Namespace get pods -l app=llbot -o json 2>$null | ConvertFrom-Json
    if ($null -eq $pods -or $pods.items.Count -eq 0) { return 'llbot pod: 不存在' }
    $pod = $pods.items[0]
    return "llbot pod: $($pod.metadata.name) phase=$($pod.status.phase) ready=$(($pod.status.containerStatuses | Where-Object { $_.ready }).Count)/$($pod.status.containerStatuses.Count)"
}

if ($To -ieq 'Status') {
    Write-Output "当前 SATORI_HOST = $(Get-CurrentHost)"
    Write-Output (Get-LlbotPodInfo)
    Write-Output ''
    Write-Output '可用目标：'
    $targets.GetEnumerator() | ForEach-Object { Write-Output ("  {0,-8} -> {1}:5600" -f $_.Key, $_.Value) }
    exit 0
}

$newHost = $targets[$To]
Write-Output "切换 SATORI_HOST -> $newHost"
kubectl -n $Namespace set env deploy/$Deployment "SATORI_HOST=$newHost" | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'set env 失败' }

if (-not $NoWait) {
    kubectl -n $Namespace rollout status deploy/$Deployment --timeout=120s | Select-Object -Last 1
}

Write-Output ''
Write-Output "切换完成，当前 SATORI_HOST = $(Get-CurrentHost)"
Start-Sleep -Seconds 6
Write-Output '--- 适配器最近日志 ---'
kubectl -n $Namespace logs deploy/$Deployment --tail=8 2>&1 |
    Select-String -Pattern 'starting|Satori|satori|上线|失败|连接' |
    Select-Object -Last 6 |
    ForEach-Object { $_.Line.Trim() }

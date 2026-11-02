# 面板图形界面真机走查（Windows 单体版 T0）
# 流程：下载源码 + 面板 → 停掉 VM 上已有实例（避免端口冲突）→ 起面板
#       → UI Automation 按 AutomationId 点按钮：初始化 → 装依赖 → 探测 → 启动 → 停止
#       → 恢复原来的实例 → 回报宿主
# 说明：全程只用 AutomationId（BtnInit / BtnInstallDeps / ...），不依赖中文控件名或像素坐标。

$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'
# 子进程（python 脚本）输出是 UTF-8，控制台默认 OEM 编码，不设就会显示成乱码
try { [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false) } catch { }

$Host8000 = 'http://10.244.37.37:8000'
$Work = Join-Path $env:USERPROFILE 'paneltest'
$Repo = Join-Path $Work 'repo'
$Data = Join-Path $Work 'data'
$AppDir = Join-Path $Work 'app'
$PanelStamp = 'p-2026-11-02-t6'
$RepoStamp = 'r-2026-11-02-t4'

$script:Ok = $true
$script:win = $null
$script:oldFeatures = ''
$script:oldModes = ''
$lines = New-Object System.Collections.Generic.List[string]

function Say($m) { $script:lines.Add([string]$m); Write-Host $m }
function Fail($m) { $script:Ok = $false; Say ("✘ " + $m) }
function Report {
    $body = @{ stage = 'panel-uia'; ok = $script:Ok; detail = ($script:lines -join "`n") } | ConvertTo-Json -Depth 3
    try {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($body)
        Invoke-WebRequest -Uri "$Host8000/report" -Method Post -Body $bytes `
            -ContentType 'application/json; charset=utf-8' -TimeoutSec 30 -UseBasicParsing | Out-Null
        Write-Host '已回报'
    } catch { Write-Host ("回报失败: " + $_.Exception.Message) }
}

# ── 下载 / 解包 ────────────────────────────────────────────────
function Get-Remote($url, $dest) {
    Say ("下载 " + $url)
    $wc = New-Object System.Net.WebClient
    if (Test-Path $dest) { Remove-Item $dest -Force }
    $wc.DownloadFile($url, $dest)
    Say ("  完成 {0:N1} MB" -f ((Get-Item $dest).Length / 1MB))
}

function Prepare-Repo {
    $stampFile = Join-Path $Repo '.repo-stamp'
    if ((Test-Path $stampFile) -and ((Get-Content $stampFile -Raw).Trim() -eq $RepoStamp)) {
        Say '源码已是当前包，跳过重新解包'
        return
    }
    if (Test-Path $Repo) { Remove-Item $Repo -Recurse -Force -ErrorAction SilentlyContinue }
    $zip = Join-Path $env:TEMP 'repo.zip'
    Get-Remote "$Host8000/repo.zip" $zip
    Expand-Archive -Path $zip -DestinationPath $Repo -Force
    Set-Content -Path (Join-Path $Repo '.repo-stamp') -Value $RepoStamp -Encoding ASCII
    Say ("源码解包到 " + $Repo + "，features.json 存在 → " + (Test-Path (Join-Path $Repo 'deploy\features.json')))
}

function Prepare-Panel {
    $stampFile = Join-Path $AppDir '.stamp'
    if ((Test-Path $stampFile) -and ((Get-Content $stampFile -Raw).Trim() -eq $PanelStamp)) {
        Say '面板已是当前构建，跳过解包'
        return
    }
    if (Test-Path $AppDir) { Remove-Item $AppDir -Recurse -Force -ErrorAction SilentlyContinue }
    $zip = Join-Path $env:TEMP 'panel.zip'
    Get-Remote "$Host8000/wheels/panel.zip" $zip
    Expand-Archive -Path $zip -DestinationPath $AppDir -Force
    Set-Content -Path (Join-Path $AppDir '.stamp') -Value $PanelStamp -Encoding ASCII
    Say ("面板解包到 " + $AppDir + "，exe 存在 → " + (Test-Path (Join-Path $AppDir 'MyAgentAdminPanel.exe')))
}

# ── UI Automation 小工具 ──────────────────────────────────────
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$AE = [System.Windows.Automation.AutomationElement]
$TS = [System.Windows.Automation.TreeScope]
$CT = [System.Windows.Automation.ControlType]

function Find-ById($root, $id) {
    $cond = New-Object System.Windows.Automation.PropertyCondition($AE::AutomationIdProperty, $id)
    return $root.FindFirst($TS::Descendants, $cond)
}

function Find-ById-Deep($id) {
    if (-not $script:win) { return $null }
    return Find-ById $script:win $id
}

function Click($id) {
    for ($i = 0; $i -lt 40; $i++) {
        $el = Find-ById-Deep $id
        if ($el) {
            try {
                if (-not $el.Current.IsEnabled) { Start-Sleep -Milliseconds 500; continue }
                $el.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke()
                return $true
            } catch { Start-Sleep -Milliseconds 500 }
        } else { Start-Sleep -Milliseconds 500 }
    }
    Fail ("点不到按钮 " + $id)
    return $false
}

function Set-Box($id, $text) {
    for ($i = 0; $i -lt 20; $i++) {
        $el = Find-ById-Deep $id
        if ($el) {
            try {
                $el.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern).SetValue($text)
                return $true
            } catch { Start-Sleep -Milliseconds 300 }
        } else { Start-Sleep -Milliseconds 300 }
    }
    Fail ("设不了输入框 " + $id)
    return $false
}

function Get-Box($id) {
    $el = Find-ById-Deep $id
    if (-not $el) { return '' }
    try { return $el.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern).Current.Value } catch { return '' }
}

function Count-Features {
    $list = Find-ById-Deep 'FeatureList'
    if (-not $list) { return -1 }
    $cond = New-Object System.Windows.Automation.PropertyCondition($AE::ControlTypeProperty, $CT::CheckBox)
    return $list.FindAll($TS::Descendants, $cond).Count
}

function Wait-Log($successText, $failureText, $seconds) {
    $deadline = (Get-Date).AddSeconds($seconds)
    $lastLen = -1
    $step = 0
    while ((Get-Date) -lt $deadline) {
        $text = Get-Box 'TxtLog'
        if ($text) {
            if ($text.Contains($successText)) { return 'ok' }
            if ($failureText -and $text.Contains($failureText)) { return 'fail' }
            if ($text.Length -ne $lastLen) {
                $lastLen = $text.Length
                $step++
                if ($step % 6 -eq 1) { Say ("  …日志 " + $text.Length + " 字符") }
            }
        }
        Start-Sleep -Seconds 5
    }
    return 'timeout'
}

function Http-Code($url) {
    try { $r = Invoke-WebRequest -Uri $url -TimeoutSec 5 -UseBasicParsing; return [int]$r.StatusCode }
    catch {
        $resp = $_.Exception.Response
        if ($resp) { try { return [int]$resp.StatusCode } catch { return 0 } }
        return 0
    }
}

function Wait-Http($url, $want, $seconds) {
    $deadline = (Get-Date).AddSeconds($seconds)
    while ((Get-Date) -lt $deadline) {
        $code = Http-Code $url
        if ($code -eq $want) { return $code }
        Start-Sleep -Seconds 3
    }
    return (Http-Code $url)
}

function Pid-Count {
    $p = Join-Path $Data 'pids.json'
    if (-not (Test-Path $p)) { return 0 }
    try { return @((Get-Content $p -Raw -Encoding UTF8 | ConvertFrom-Json).processes).Count } catch { return -1 }
}

$oldData = Join-Path $env:LOCALAPPDATA 'MyAgent\data'

# ── 主流程 ────────────────────────────────────────────────────
try {
    Say '=== 0. 准备 ==='
    Prepare-Repo
    Prepare-Panel

    Say '=== 0.5 停掉 VM 上已有实例（新用户机器上没有旧实例，端口 5210/5700/5102 会撞） ==='
    $oldRuntime = Join-Path $oldData 'runtime.json'
    if (Test-Path $oldRuntime) {
        try {
            $rt = Get-Content $oldRuntime -Raw -Encoding UTF8 | ConvertFrom-Json
            $script:oldFeatures = (@($rt.features) -join ',')
            if ($rt.PSObject.Properties.Name -contains 'modes') {
                $m = $rt.modes
                if ($m -is [string]) { $script:oldModes = $m }
                elseif ($m) { $script:oldModes = (($m.PSObject.Properties | ForEach-Object { $_.Name + '=' + $_.Value }) -join ',') }
            }
            Say ("  旧实例 features=" + $script:oldFeatures + " modes=" + $script:oldModes)
        } catch { Say ("  读旧 runtime.json 失败：" + $_.Exception.Message) }
    }
    if (Test-Path (Join-Path $oldData 'pids.json')) {
        & python (Join-Path $Repo 'deploy\launcher\run.py') --data-dir $oldData --stop 2>&1 |
            ForEach-Object { Say ("  " + $_) }
        Start-Sleep -Seconds 3
        Say ("  停完后 :5210 → " + (Http-Code 'http://127.0.0.1:5210/api/health'))
    } else { Say '  没有旧实例在跑' }
    Get-Process MyAgentAdminPanel -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue

    Say '=== 1. 启动面板 ==='
    $proc = Start-Process -FilePath (Join-Path $AppDir 'MyAgentAdminPanel.exe') -WorkingDirectory $AppDir -PassThru
    Say ("面板进程 pid " + $proc.Id)

    $cond = New-Object System.Windows.Automation.PropertyCondition($AE::ProcessIdProperty, $proc.Id)
    $deadline = (Get-Date).AddSeconds(90)
    while ((Get-Date) -lt $deadline) {
        $script:win = $AE::RootElement.FindFirst($TS::Children, $cond)
        if ($script:win) { break }
        Start-Sleep -Milliseconds 500
    }
    if (-not $script:win) { throw '面板窗口没出现（90s）' }
    Say ("拿到窗口：" + $script:win.Current.Name)

    Say '=== 2. 进「单体模式」页 ==='
    if (-not (Click 'BtnStandalone')) { throw '进不了单体模式页' }
    Start-Sleep -Seconds 2
    if (-not (Find-ById-Deep 'TxtRepoRoot')) { throw '单体页没加载（找不到 TxtRepoRoot）' }
    Say ("页面默认仓库根：" + (Get-Box 'TxtRepoRoot'))
    Say ("页面默认数据目录：" + (Get-Box 'TxtDataDir'))

    Say '=== 3. 改成隔离的路径 ==='
    Set-Box 'TxtRepoRoot' $Repo | Out-Null
    Set-Box 'TxtDataDir' $Data | Out-Null
    Say ("仓库根 → " + (Get-Box 'TxtRepoRoot'))
    Say ("数据目录 → " + (Get-Box 'TxtDataDir'))
    Click 'BtnRefresh' | Out-Null
    Start-Sleep -Seconds 2
    $featureCount = Count-Features
    Say ("功能勾选项数 → " + $featureCount)
    if ($featureCount -lt 5) { Fail '功能清单没加载出来（features.json 没读到）' }

    Say '=== 4. 点「初始化数据目录」 ==='
    if (-not (Click 'BtnInit')) { throw '点不到初始化' }
    $r = Wait-Log '✔ 初始化数据目录完成' '✘ 初始化数据目录失败' 300
    Say ("结果：" + $r)
    if ($r -ne 'ok') { Fail '初始化没成功' }
    Say ("runtime.json 存在 → " + (Test-Path (Join-Path $Data 'runtime.json')))
    Say ("config 目录存在 → " + (Test-Path (Join-Path $Data 'config')))
    Say ("launcher.json 存在 → " + (Test-Path (Join-Path $Data 'config\launcher.json')))

    Say '=== 5. 点「安装依赖（共享 venv）」 ==='
    if (-not (Click 'BtnInstallDeps')) { throw '点不到装依赖' }
    $r = Wait-Log '✔ 安装依赖完成' '✘ 安装依赖失败' 2400
    Say ("结果：" + $r)
    if ($r -ne 'ok') { Fail '装依赖没成功' }
    Say ("venv python 存在 → " + (Test-Path (Join-Path $Data 'venv\Scripts\python.exe')))
    Say ("deps stamp 存在 → " + (Test-Path (Join-Path $Data 'venv\.deps-stamp.json')))

    Say '=== 6. 点「探测环境」 ==='
    if (-not (Click 'BtnProbe')) { throw '点不到探测' }
    $r = Wait-Log '✔ 探测环境完成' '✘ 探测环境失败' 240
    Say ("结果：" + $r)
    if ($r -ne 'ok') { Fail '探测没成功' }
    $cap = Join-Path $Data 'capability.json'
    Say ("capability.json 存在 → " + (Test-Path $cap))
    if (Test-Path $cap) {
        $c = Get-Content $cap -Raw -Encoding UTF8 | ConvertFrom-Json
        Say ("  runtime_tier = " + $c.runtime_tier + "，degraded 数 = " + (@($c.degraded).Count))
    }

    Say '=== 7. 点「启动」 ==='
    if (-not (Click 'BtnStart')) { throw '点不到启动' }
    $code = Wait-Http 'http://127.0.0.1:5210/api/health' 200 240
    Say ("网关 :5210/api/health → " + $code)
    if ($code -ne 200) { Fail '启动后网关没通' }
    $pc = 0
    for ($i = 0; $i -lt 30; $i++) { $pc = Pid-Count; if ($pc -gt 0) { break }; Start-Sleep -Seconds 2 }
    Say ("pids.json 进程数 → " + $pc)
    if ($pc -lt 1) { Fail '启动后 pids.json 里没有进程' }
    Say ("dashboard :5700 → " + (Http-Code 'http://127.0.0.1:5700/'))
    Say ("assets :5102 → " + (Http-Code 'http://127.0.0.1:5102/assets/'))
    Start-Sleep -Seconds 3
    Say '--- 状态区 ---'
    foreach ($line in ((Get-Box 'TxtStatus') -split "`r?`n")) { Say ("  " + $line) }

    Say '=== 8. 点「停止」 ==='
    if (-not (Click 'BtnStop')) { throw '点不到停止' }
    $deadline = (Get-Date).AddSeconds(90)
    $gone = $false
    while ((Get-Date) -lt $deadline) {
        if ((Http-Code 'http://127.0.0.1:5210/api/health') -ne 200) { $gone = $true; break }
        Start-Sleep -Seconds 3
    }
    Say ("网关已停 → " + $gone)
    if (-not $gone) { Fail '停止后网关还在' }
    Say ("pids.json 还在 → " + (Test-Path (Join-Path $Data 'pids.json')))

    Say '--- 日志区（末尾）---'
    $log = Get-Box 'TxtLog'
    if ($log) {
        foreach ($line in ($log -split "`r?`n" | Select-Object -Last 30)) { Say ("  " + $line) }
    }
} catch {
    Fail ("异常：" + $_.Exception.Message)
} finally {
    Say ("=== 结论：" + $(if ($script:Ok) { '面板全流程通过' } else { '有失败项，见上' }) + " ===")
    Get-Process MyAgentAdminPanel -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    New-Item -ItemType Directory -Force -Path $Work | Out-Null
    if (Test-Path $Data) {
        Copy-Item (Join-Path $Data 'logs\launcher.log') (Join-Path $Work 'launcher.log') -Force -ErrorAction SilentlyContinue
    }
    $script:lines | Set-Content (Join-Path $Work 'panel-uia.log') -Encoding UTF8

    # 恢复 VM 原来的单体实例，别把环境留成"停着的"
    if ($script:oldFeatures) {
        Say '=== 9. 恢复 VM 原来的单体实例 ==='
        $argList = @((Join-Path $Repo 'deploy\launcher\run.py'), '--data-dir', $oldData, '--features', $script:oldFeatures)
        if ($script:oldModes) { $argList += @('--modes', $script:oldModes) }
        Start-Process -FilePath 'python' -ArgumentList $argList -WindowStyle Hidden | Out-Null
        Say ("  恢复后 :5210 → " + (Wait-Http 'http://127.0.0.1:5210/api/health' 200 180))
    }
    Report
}

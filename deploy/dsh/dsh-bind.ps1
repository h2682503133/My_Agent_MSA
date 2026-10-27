<#
.SYNOPSIS
    Bind the OneBot chat to a chosen dsh session (by session title or id).

.DESCRIPTION
    dsh-onebot-qq keeps chat->session binding in
        %USERPROFILE%\.dsh\media\onebot\chat-sessions.json
    and reads it ONLY at plugin mount, so after editing you must restart dsh.

    This script resolves the target session from .dsh\storages\session_projcache.json
    (title / cwd / turns / seq; larger seq = more recently active) and rewrites the
    binding file. workspacePath is intentionally dropped so the /workspace command
    never hijacks the bound session's cwd.

    NOTE: keep this file ASCII-only (Windows PowerShell 5.1 reads .ps1 as ANSI
    unless it has a BOM).

.PARAMETER List
    List sessions (id, turns, cwd, title) sorted by activity.

.PARAMETER Title
    Session title keyword (contains, case-insensitive).

.PARAMETER Id
    Session id or unique id prefix.

.PARAMETER Cwd
    Filter by session working directory (substring).

.PARAMETER Latest
    When several sessions match, take the most recently active one.

.PARAMETER Apply
    Actually write the binding file (default is a dry run).

.PARAMETER Restart
    Restart dsh web after writing (detached, survives this script).

.EXAMPLE
    .\dsh-bind.ps1 -List

.EXAMPLE
    .\dsh-bind.ps1 -Title "<title keyword>" -Cwd "<dir name>" -Apply -Restart
#>
[CmdletBinding()]
param(
    [switch]$List,
    [string]$Title,
    [string]$Id,
    [string]$Cwd,
    [switch]$Latest,
    [switch]$Apply,
    [switch]$Restart,
    [switch]$RestartOnly,
    [string]$Chat = ""
)

$ErrorActionPreference = 'Stop'

$dshHome = Join-Path $env:USERPROFILE '.dsh'
$cache   = Join-Path $dshHome 'storages\session_projcache.json'
$mapping = Join-Path $dshHome 'media\onebot\chat-sessions.json'

function Restart-DshWeb {
    param([scriptblock]$BeforeStart)
    $proc = Get-CimInstance Win32_Process -Filter "Name='node.exe'" |
        Where-Object { $_.CommandLine -like '*dsh*bin.js*web*' } | Select-Object -First 1
    if (-not $proc) { throw "no running dsh web process found" }
    # derive node.exe and bin.js from the running process command line
    $cmd = [string]$proc.CommandLine
    $node = if ($cmd -match '^\s*"([^"]+node(?:\.exe)?)"') { $Matches[1] } else { 'node' }
    $binPath = if ($cmd -match '([A-Za-z]:\\[^"]*?bin\.js)') { $Matches[1] } else { 'dsh' }
    $argsPart = ($cmd -split 'bin\.js', 2)[1]

    Write-Host "stopping dsh web (pid $($proc.ProcessId))..." -ForegroundColor Yellow
    Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 3

    # dsh is down now: nothing can overwrite the mapping behind our back
    if ($BeforeStart) { & $BeforeStart }

    Write-Host "starting dsh web..." -ForegroundColor Green
    Start-Process -FilePath $node -ArgumentList "$binPath$argsPart"
}

# chat key is required for -Apply (no project-specific default)
if ($Apply -and $Chat -notmatch '^(group|private):[0-9]+$') {
    throw "pass -Chat <group:digits|private:digits> (the OneBot chat id to bind)"
}

if ($RestartOnly) {
    Restart-DshWeb
    return
}

if (-not (Test-Path $cache))   { throw "session cache not found: $cache" }
if (-not (Test-Path $mapping)) { throw "mapping file not found: $mapping (start dsh-onebot once first)" }

# ---- read session metadata -------------------------------------------------
$json = Get-Content $cache -Raw -Encoding UTF8 | ConvertFrom-Json
$sessions = @()
foreach ($prop in $json.tables.sessions.PSObject.Properties) {
    $rec  = $prop.Value
    $rows = $rec.rows
    $sesTitle = if ($rows.title) { [string]$rows.title.val } else { '' }
    $seq = 0
    foreach ($r in @($rows.PSObject.Properties)) {
        if ($r.Value.seq -and [int64]$r.Value.seq -gt $seq) { $seq = [int64]$r.Value.seq }
    }
    $turns = 0
    if ($rows.sessionStats -and $rows.sessionStats.val) { $turns = [int]$rows.sessionStats.val.turns }
    $sessions += [pscustomobject]@{
        Id     = $prop.Name
        Short  = $prop.Name.Substring(0, [Math]::Min(19, $prop.Name.Length))
        Cwd    = [string]$rec.identity.cwd
        Title  = $sesTitle
        Turns  = $turns
        Seq    = $seq
    }
}
$sessions = $sessions | Sort-Object -Property Seq -Descending

function Show-Sessions($items) {
    $items | Select-Object @{n='id';e={$_.Short}}, @{n='turns';e={$_.Turns}}, @{n='cwd';e={$_.Cwd}}, @{n='title';e={$_.Title}} |
        Format-Table -AutoSize | Out-String -Width 200 | Write-Host
}

if ($List -or (-not $Title -and -not $Id)) {
    Write-Host ""
    Write-Host "=== most recently active sessions ===" -ForegroundColor Cyan
    Show-Sessions ($sessions | Select-Object -First 15)
    Write-Host "usage: .\dsh-bind.ps1 -Title <keyword> -Apply   (or -Id <prefix>)" -ForegroundColor DarkGray
    if ($List) { return }
}

# ---- pick the target -------------------------------------------------------
$candidates = $sessions
if ($Cwd) { $candidates = $candidates | Where-Object { $_.Cwd -like "*$Cwd*" } }
if ($Id) {
    $candidates = $candidates | Where-Object { $_.Id -like "$Id*" }
}
elseif ($Title) {
    $candidates = $candidates | Where-Object { $_.Title -like "*$Title*" }
}

if (-not $candidates) { throw "no session matched (Title='$Title' Id='$Id' Cwd='$Cwd')" }

if ($candidates.Count -gt 1) {
    if (-not $Latest) {
        Write-Host ""
        Write-Host "several sessions matched; narrow with -Cwd or add -Latest:" -ForegroundColor Yellow
        Show-Sessions $candidates
        return
    }
    Write-Host "matched $($candidates.Count) sessions, -Latest picks the most active one" -ForegroundColor Yellow
}

$target = $candidates | Select-Object -First 1
Write-Host ""
Write-Host "=== target session ===" -ForegroundColor Cyan
Show-Sessions @($target)

# ---- write binding ---------------------------------------------------------
$map = Get-Content $mapping -Raw -Encoding UTF8 | ConvertFrom-Json
$newMap = [ordered]@{}
foreach ($p in $map.PSObject.Properties) {
    # rewrite only the target chat; workspace-derived chats keep their own session
    if ($p.Name -eq $Chat) {
        $newMap[$p.Name] = [ordered]@{ session = $target.Id }
    }
    else {
        $newMap[$p.Name] = $p.Value
    }
}
if (-not $newMap.Contains($Chat)) {
    $newMap[$Chat] = [ordered]@{ session = $target.Id }
}
if ($newMap.Count -eq 0) { throw "binding file has no chat entry (bot never received a message?)" }

$content = $newMap | ConvertTo-Json -Depth 5
Write-Host ""
Write-Host "=== will write to $mapping ===" -ForegroundColor Cyan
Write-Host $content

if (-not $Apply) {
    Write-Host ""
    Write-Host "(dry run; add -Apply to write, then dsh must be restarted)" -ForegroundColor Yellow
    return
}

$writeAction = {
    $backup = "$mapping.bak-{0}" -f (Get-Date -Format 'yyyyMMdd-HHmmss')
    Copy-Item $mapping $backup -ErrorAction SilentlyContinue
    # atomic write: temp file then replace, so a concurrent dsh start never sees an empty file
    $tmp = "$mapping.tmp-$PID"
    # no BOM: PowerShell 5.1's Set-Content -Encoding UTF8 writes one, and the plugin's
    # JSON.parse then throws (silently caught -> 0 resumed chats -> it saves "{}")
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($tmp, $content, $utf8NoBom)
    Move-Item -Path $tmp -Destination $mapping -Force
    Write-Host "written (backup: $(Split-Path $backup -Leaf))" -ForegroundColor Green
}.GetNewClosure()

if ($Restart) {
    # stop first, then write, then start: the plugin must never see a stale/empty file
    Restart-DshWeb -BeforeStart $writeAction
}
else {
    & $writeAction
    $dshProc = Get-CimInstance Win32_Process -Filter "Name='node.exe'" |
        Where-Object { $_.CommandLine -like '*dsh*bin.js*web*' } | Select-Object -First 1
    if ($dshProc) {
        Write-Host "dsh is still running: restart it to load the new binding (-Restart does this)" -ForegroundColor Yellow
    }
}


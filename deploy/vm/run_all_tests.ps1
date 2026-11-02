# 顺序跑仓库内全部 *_test.py，逐条把输出落文件，最后写一份汇总。
# 目的：避免在宿主一次性把大量测试输出灌进同一个会话（曾把 DSH 拖崩）。
$ErrorActionPreference = "Continue"
$repo = "E:\github\My_Agent_MSA"
$outDir = Join-Path $env:TEMP "myagent-regression"
New-Item -ItemType Directory -Force -Path $outDir | Out-Null

$tests = Get-ChildItem -Path $repo -Recurse -File -Filter '*_test.py' -ErrorAction SilentlyContinue |
    Where-Object { $_.FullName -notmatch '\\deploy\\vm\\' } |
    Sort-Object FullName

$summary = Join-Path $outDir "summary.txt"
"# regression start $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  total=$($tests.Count)" | Set-Content $summary -Encoding UTF8

$pass = 0
$fail = 0
foreach ($t in $tests) {
    $rel = $t.FullName.Replace("$repo\", "")
    $log = Join-Path $outDir (($rel -replace '[\\/:*?"<>|]', '_') + ".log")
    try {
        & python -X utf8 $t.FullName *> $log
        $code = $LASTEXITCODE
    } catch {
        $code = 999
        "harness error: $($_.Exception.Message)" | Add-Content $log
    }
    $text = ""
    if (Test-Path $log) { $text = (Get-Content $log -Raw -ErrorAction SilentlyContinue) }
    $ok = ($code -eq 0) -or (
        ($text -match 'ALL OK|全部通过|PASS|OK \d+ checks|done: \d+/\d+ passed|FAILURES: none') -and
        ($text -notmatch 'FAILED: [1-9]|FAILURES: [^n]|Traceback|AssertionError|ModuleNotFoundError')
    )
    if ($ok) { $pass++ } else { $fail++ }
    $tag = if ($ok) { "PASS" } else { "FAIL" }
    "$tag`t$rel`t exit=$code" | Add-Content $summary
}

"# done $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  pass=$pass fail=$fail" | Add-Content $summary
Write-Host "SUMMARY $summary"
Get-Content $summary | Where-Object { $_ -match '^FAIL|^# done' }

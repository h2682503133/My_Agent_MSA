param([string]$FreshDir = "C:\MyAgentFreshData", [string]$ApiKey = "6fca3fd5-5b84-4c05-a232-dd816910d632")
$ErrorActionPreference = "Continue"
$p = Join-Path $FreshDir "config\model-proxy\config\model_list.json"
if (-not (Test-Path $p)) { Write-Host ("找不到模型配置: " + $p); exit 1 }
$json = Get-Content $p -Raw -Encoding UTF8 | ConvertFrom-Json
$patched = @()
foreach ($name in $json.models.PSObject.Properties.Name) {
    $m = $json.models.$name
    if ($m.PSObject.Properties.Name -contains "api_key" -and $m.api_key -like "replace-*") {
        $m.api_key = $ApiKey
        $patched += $name
    }
}
$json | ConvertTo-Json -Depth 8 | Set-Content $p -Encoding UTF8
Write-Host ("已写入真实 key 的模型: " + ($patched -join ", "))
Write-Host ("文件: " + $p)

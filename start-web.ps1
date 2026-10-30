param([int]$Port = 8765)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$python = if (Test-Path '.venv\Scripts\python.exe') { (Resolve-Path '.venv\Scripts\python.exe').Path } else { (Get-Command python -ErrorAction Stop).Source }
if (-not (Test-Path 'web\dist\index.html')) {
    throw 'Build frontend first: cd web; bun install; bun run build'
}
while (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) {
    try {
        $status = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/status" -TimeoutSec 2
        if ($status.application -eq 'douyin-live-assistant' -and $status.collector.status -ne 'isolated_test') {
            Write-Host "Live assistant already running: http://127.0.0.1:$Port"
            exit 0
        }
    } catch { }
    $Port++
}
Write-Host "Live assistant: http://127.0.0.1:$Port"
& $python -X utf8 -m assistant_app --port $Port

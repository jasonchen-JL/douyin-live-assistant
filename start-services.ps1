param(
    [ValidateSet('Live', 'Demo', 'All')]
    [string]$Mode = 'Live',
    [switch]$OpenBrowser
)

$ErrorActionPreference = 'Stop'
$serviceRoot = $PSScriptRoot

function Get-AssistantStatus([int]$Port) {
    try {
        return Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/status" -TimeoutSec 2
    } catch {
        return $null
    }
}

function Confirm-ServiceIdentity($Status, [bool]$Demo) {
    return $Status -and $Status.application -eq 'douyin-live-assistant' -and
        (($Status.collector.status -eq 'isolated_test') -eq $Demo) -and
        $Status.analysis_commands_enabled
}

function Start-AssistantService([int]$Port, [string[]]$LaunchArgs, [bool]$Demo) {
    $url = "http://127.0.0.1:$Port/"
    $status = Get-AssistantStatus $Port
    if ($status) {
        if (-not (Confirm-ServiceIdentity $status $Demo)) {
            throw "Port $Port belongs to a different service. Nothing was stopped."
        }
        Write-Host "Already running: $url"
    } else {
        if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) {
            throw "Port $Port is occupied or the service is not responding. Nothing was stopped."
        }
        if (-not (Test-Path -LiteralPath (Join-Path $serviceRoot 'web\dist\index.html'))) {
            throw 'Frontend is not built. Run bun install and bun run build inside web first.'
        }
        $servicePython = Join-Path $serviceRoot '.venv\Scripts\python.exe'
        if (-not (Test-Path -LiteralPath $servicePython)) {
            $pythonCommand = Get-Command python -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
            if (-not $pythonCommand) { throw 'Python 3.11+ was not found. Install the requirements first.' }
            $servicePython = $pythonCommand.Source
        }
        & $servicePython -c 'import sys,fastapi,uvicorn,httpx,keyring; assert sys.version_info >= (3,11)'
        if ($LASTEXITCODE -ne 0) { throw 'Python dependencies are missing. Run python -m pip install -r requirements.txt.' }

        $runtimePath = Join-Path $serviceRoot 'runtime'
        New-Item -ItemType Directory -Path $runtimePath -Force | Out-Null
        $logBase = Join-Path $runtimePath ("launcher-$Port-" + (Get-Date -Format 'yyyyMMdd-HHmmss-fff'))
        $child = Start-Process -FilePath $servicePython -ArgumentList $LaunchArgs `
            -WorkingDirectory $serviceRoot -WindowStyle Hidden `
            -RedirectStandardOutput "$logBase.stdout.log" -RedirectStandardError "$logBase.stderr.log" -PassThru
        $deadline = [DateTime]::UtcNow.AddSeconds(40)
        do {
            $status = Get-AssistantStatus $Port
            if ($status) {
                if (-not (Confirm-ServiceIdentity $status $Demo)) {
                    throw "Unexpected service on port $Port. Check $logBase.stderr.log"
                }
                break
            }
            if ($child.HasExited) { throw "Service exited. Check $logBase.stderr.log" }
            Start-Sleep -Milliseconds 400
        } while ([DateTime]::UtcNow -lt $deadline)
        if (-not $status) { throw "Service did not become ready in 40 seconds. Check $logBase.stderr.log" }
        Write-Host "Started: $url"
        Write-Host "Log: $logBase.stderr.log"
    }
    Write-Host "Stored comments: $($status.comments)"
    if ($Demo) {
        Write-Host 'Demo uses local simulated AI. If the list is empty, include an earlier date in the date filter.'
    } else {
        Write-Host "Collector state: $($status.collector.source_state)"
        Write-Host 'When live, use Start listening in the workbench. Keep the collector browser open.'
    }
    if ($OpenBrowser) { Start-Process $url | Out-Null }
}

if ($Mode -in @('Live', 'All')) {
    Start-AssistantService -Port 8765 -LaunchArgs @('-X', 'utf8', '-m', 'assistant_app', '--port', '8765') -Demo $false
}
if ($Mode -in @('Demo', 'All')) {
    Start-AssistantService -Port 8768 -LaunchArgs @('-X', 'utf8', '-m', 'tests.workbench_demo', '--port', '8768') -Demo $true
}

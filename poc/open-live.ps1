param(
    [string]$RoomUrl = 'https://live.douyin.com/100000000001',
    [switch]$Close,
    [switch]$TestAccount,
    [switch]$SaveState
)

$ErrorActionPreference = 'Stop'
$roomUri = [uri]$RoomUrl
if ($roomUri.Scheme -ne 'https' -or $roomUri.Host -ne 'live.douyin.com' -or
    $roomUri.AbsolutePath -notmatch '^/([0-9]+)/?$') {
    throw 'Expected https://live.douyin.com/<numeric-room-id>'
}
$roomId = $Matches[1]
$targetUrl = 'https://live.douyin.com/' + $roomId
$sessionName = 'douyin-assistant-' + $roomId
if ($TestAccount) { $sessionName = 'douyin-assistant-sender' }
$profilePath = Join-Path $PSScriptRoot ('runtime/' + $sessionName + '/profile')
$configPath = Join-Path $PSScriptRoot 'cli.config.json'
$cli = Get-Command playwright-cli -ErrorAction Stop

Push-Location $PSScriptRoot
try {
    if ($Close) {
        & $cli.Source "-s=$sessionName" close
        if ($LASTEXITCODE -ne 0) { throw 'Failed to close Playwright session' }
        return
    }
    New-Item -ItemType Directory -Path $profilePath -Force | Out-Null
    $listing = & $cli.Source list --json
    if ($LASTEXITCODE -ne 0) { throw 'Could not list Playwright sessions' }
    $browsers = ($listing -join "`n" | ConvertFrom-Json).browsers
    $existing = @($browsers | Where-Object { $_.name -eq $sessionName -and $_.status -eq 'open' })
    if ($existing.Count -gt 0) {
        $currentRaw = & $cli.Source "-s=$sessionName" --raw eval location.href
        $currentUri = [uri](($currentRaw -join "`n") | ConvertFrom-Json)
        if ($currentUri.GetLeftPart([System.UriPartial]::Path).TrimEnd('/') -ne $targetUrl) {
            & $cli.Source "-s=$sessionName" goto $targetUrl
        }
    } else {
        & $cli.Source "-s=$sessionName" open $targetUrl --headed "--config=$configPath" "--profile=$profilePath"
    }
    if ($LASTEXITCODE -ne 0) { throw 'Could not open the live room' }
    $actualRaw = & $cli.Source "-s=$sessionName" --raw eval location.href
    $actualUri = [uri](($actualRaw -join "`n") | ConvertFrom-Json)
    if ($actualUri.GetLeftPart([System.UriPartial]::Path).TrimEnd('/') -ne $targetUrl) {
        throw 'The browser did not reach the requested room'
    }
    if ($SaveState) {
        $statePath = Join-Path $PSScriptRoot ('runtime/' + $sessionName + '/login.auth-state.json')
        & $cli.Source "-s=$sessionName" state-save $statePath
        if ($LASTEXITCODE -ne 0) { throw 'Could not save browser state' }
        Write-Host "Local state backup: $statePath"
    }
    Write-Host "Session: $sessionName"
    Write-Host "Room: $targetUrl"
    Write-Host "Persistent profile: $profilePath"
} finally {
    Pop-Location
}

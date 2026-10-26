param(
    [string]$GuestHost = '172.26.1.3',
    [string]$GuestUser = 'root',
    [string]$KeyPath,
    [ValidateRange(1024, 65535)][int]$LocalPort = 8899,
    [switch]$OpenBrowser
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
if ($KeyPath) { $KeyPath = (Resolve-Path -LiteralPath $KeyPath).Path }
$ssh = (Get-Command ssh -ErrorAction Stop).Source
$sshOptions = @('-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
    '-o', 'StrictHostKeyChecking=yes')
if ($KeyPath) { $sshOptions += @('-i', $KeyPath) }
$destination = "${GuestUser}@${GuestHost}"

& $ssh @sshOptions $destination 'systemctl start oe-live-dashboard.service'
if ($LASTEXITCODE -ne 0) {
    throw 'Could not start the dashboard service on the guest.'
}

$url = "http://127.0.0.1:$LocalPort/"
$stateUrl = "${url}api/live-state"
$ready = $false
try {
    $state = Invoke-RestMethod -Uri $stateUrl -TimeoutSec 2 -ErrorAction Stop
    $ready = $state.ok -and $null -ne $state.scheduler
} catch {}

if (-not $ready) {
    if (Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort $LocalPort -State Listen -ErrorAction SilentlyContinue) {
        throw "Local port $LocalPort is already in use. Choose another -LocalPort."
    }
    $stateDir = Join-Path $projectRoot 'state'
    New-Item -ItemType Directory -Path $stateDir -Force | Out-Null
    $arguments = @('-N', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
        '-o', 'StrictHostKeyChecking=yes', '-o', 'ExitOnForwardFailure=yes',
        '-o', 'ServerAliveInterval=30', '-o', 'ServerAliveCountMax=3',
        '-L', "127.0.0.1:${LocalPort}:127.0.0.1:8899", $destination)
    if ($KeyPath) { $arguments = @('-i', ('"' + $KeyPath + '"')) + $arguments }
    $tunnel = Start-Process -FilePath $ssh -ArgumentList $arguments -WindowStyle Hidden -PassThru `
        -RedirectStandardError (Join-Path $stateDir 'dashboard-tunnel.stderr.log') `
        -RedirectStandardOutput (Join-Path $stateDir 'dashboard-tunnel.stdout.log')
    $deadline = (Get-Date).AddSeconds(20)
    while ((Get-Date) -lt $deadline) {
        $tunnel.Refresh()
        if ($tunnel.HasExited) { break }
        try {
            $state = Invoke-RestMethod -Uri $stateUrl -TimeoutSec 2 -ErrorAction Stop
            if ($state.ok -and $null -ne $state.scheduler) { $ready = $true; break }
        } catch {}
        Start-Sleep -Milliseconds 500
    }
    if (-not $ready) {
        if (-not $tunnel.HasExited) { Stop-Process -Id $tunnel.Id }
        throw 'Dashboard did not become ready. Check state/dashboard-tunnel.stderr.log and the guest journal.'
    }
    @{ pid = $tunnel.Id; guest = $GuestHost; local_port = $LocalPort; url = $url } |
        ConvertTo-Json | Set-Content -LiteralPath (Join-Path $stateDir 'dashboard-tunnel.json') -Encoding UTF8
}

Write-Output "Dashboard ready: $url"
Write-Output 'The guest service listens on loopback; this SSH tunnel provides browser access.'
if ($OpenBrowser) { Start-Process $url }

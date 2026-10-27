param(
    [string]$GuestHost = '192.168.200.130',
    [string]$KeyPath,
    [switch]$Once
)
$ErrorActionPreference = 'Stop'
if (-not $KeyPath) {
    $projectRoot = Split-Path -Parent $PSScriptRoot
    $workspaceRoot = Split-Path -Parent (Split-Path -Parent $projectRoot)
    $KeyPath = Join-Path $workspaceRoot '.vm-access/openeuler_ed25519'
}
$KeyPath = (Resolve-Path -LiteralPath $KeyPath).Path
$remoteCommand = 'bash /root/oe-resource-agent/scripts/watch-demo.sh'
if ($Once) { $remoteCommand += ' --once' }
& ssh -tt -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=yes `
    -i $KeyPath "root@$GuestHost" $remoteCommand
exit $LASTEXITCODE

param(
    [string]$GuestHost = '172.26.1.3',
    [string]$KeyPath,
    [switch]$Once
)
$ErrorActionPreference = 'Stop'
$sshOptions = @('-tt', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
    '-o', 'StrictHostKeyChecking=yes')
if ($KeyPath) {
    $KeyPath = (Resolve-Path -LiteralPath $KeyPath).Path
    $sshOptions += @('-i', $KeyPath)
}
$remoteCommand = 'bash /root/oe-resource-agent/scripts/watch-demo.sh'
if ($Once) { $remoteCommand += ' --once' }
& ssh @sshOptions "root@$GuestHost" $remoteCommand
exit $LASTEXITCODE

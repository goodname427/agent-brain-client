# Native Windows entry. Uses normal per-user Python/Git and Codex hook review.
[CmdletBinding()]
param(
    [ValidateSet('install','status','repair','uninstall','check-update','update')]
    [string]$Action = 'install',
    [string]$Connection,
    [string]$ExpectedConnectionSha256
)
$ErrorActionPreference = 'Stop'
if ($env:OS -ne 'Windows_NT') { throw 'Native Windows required; WSL uses a different OS and identity.' }
$root = Split-Path -Parent $PSScriptRoot
$pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
if (-not $pythonCommand) { throw 'Install Python 3.11+ and Git for this user, then rerun. See docs/FIRST-INSTALL-WINDOWS.md.' }
$python = & $pythonCommand.Source -I -B -c 'import sys; assert sys.version_info >= (3,11); print(sys.executable)'
if ($LASTEXITCODE -ne 0) { throw 'Python 3.11+ required.' }
$arguments = @('--action', $Action)
if ($Connection) { $arguments += @('--connection', $Connection) }
if ($ExpectedConnectionSha256) { $arguments += @('--expected-connection-sha256', $ExpectedConnectionSha256) }
if ($Action -eq 'install') {
    $checkout = & $python -I -B (Join-Path $root 'tools/bootstrap_main.py') --ensure
    if ($LASTEXITCODE -ne 0) { throw 'Fresh official checkout could not be verified; preserve existing installation.' }
    if ($checkout -ne $root) {
        & (Join-Path $checkout 'tools/install-windows.ps1') -Action $Action -Connection $Connection -ExpectedConnectionSha256 $ExpectedConnectionSha256
        exit $LASTEXITCODE
    }
}
& $python -I -B (Join-Path $root 'tools/mac_first_install.py') @arguments
exit $LASTEXITCODE

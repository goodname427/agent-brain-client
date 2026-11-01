# Native Windows entry. Uses normal per-user Python/Git and Codex hook review.
[CmdletBinding()]
param(
    [ValidateSet('install','status','repair','uninstall','check-update','update')]
    [string]$Action = 'install',
    [string]$Server,
    [string]$Connection,
    [string]$ExpectedConnectionSha256
)
$ErrorActionPreference = 'Stop'
$previousConsoleEncoding = [Console]::OutputEncoding
$previousPipeEncoding = $OutputEncoding
try {
    [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
    $OutputEncoding = [Console]::OutputEncoding
    if ($env:OS -ne 'Windows_NT') { throw 'Native Windows required; WSL uses a different OS and identity.' }
    $root = Split-Path -Parent $PSScriptRoot
    . (Join-Path $PSScriptRoot 'windows-python.ps1')
    $python = Resolve-AgentBrainPython
    $arguments = @('--action', $Action)
    if ($Server) { $arguments += @('--server', $Server) }
    if ($Connection) { $arguments += @('--connection', $Connection) }
    if ($ExpectedConnectionSha256) { $arguments += @('--expected-connection-sha256', $ExpectedConnectionSha256) }
    if ($Action -eq 'install') {
        $checkout = & $python -X utf8 -I -B (Join-Path $root 'tools/bootstrap_main.py') --ensure
        if ($LASTEXITCODE -ne 0) { throw 'Fresh official checkout could not be verified; preserve existing installation.' }
        if ($checkout -ne $root) {
            & (Join-Path $checkout 'tools/install-windows.ps1') -Action $Action -Server $Server -Connection $Connection -ExpectedConnectionSha256 $ExpectedConnectionSha256
            exit $LASTEXITCODE
        }
    }
    & $python -X utf8 -I -B (Join-Path $root 'tools/mac_first_install.py') @arguments
    exit $LASTEXITCODE
} finally {
    [Console]::OutputEncoding = $previousConsoleEncoding
    $OutputEncoding = $previousPipeEncoding
}

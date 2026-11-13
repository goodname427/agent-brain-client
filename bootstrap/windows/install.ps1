# Native Windows entry. Uses normal per-user Python/Git and Codex hook review.
[CmdletBinding()]
param(
    [ValidateSet('install','reinstall','reinstall-check','status','repair','uninstall','check-update','update')]
    [string]$Action = 'install',
    [ValidateSet('codex','codebuddy')]
    [string]$Harness = 'codex',
    [string]$Server,
    [string]$Connection,
    [string]$ExpectedConnectionSha256
)
$ErrorActionPreference = 'Stop'
$Action = $Action.ToLowerInvariant()
$previousConsoleEncoding = [Console]::OutputEncoding
$previousPipeEncoding = $OutputEncoding
try {
    [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
    $OutputEncoding = [Console]::OutputEncoding
    if ($env:OS -ne 'Windows_NT') { throw 'Native Windows required; WSL uses a different OS and identity.' }
    $root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
    . (Join-Path $PSScriptRoot 'python.ps1')
    $python = Resolve-AgentBrainPython
    $arguments = @('--action', $Action, '--harness', $Harness)
    if ($Server) { $arguments += @('--server', $Server) }
    if ($Connection) { $arguments += @('--connection', $Connection) }
    if ($ExpectedConnectionSha256) { $arguments += @('--expected-connection-sha256', $ExpectedConnectionSha256) }
    if ($Action -in @('install', 'reinstall')) {
        $checkout = & $python -X utf8 -I -B (Join-Path $root 'bootstrap/source_checkout.py') --ensure
        if ($LASTEXITCODE -ne 0) { throw 'Fresh official checkout could not be verified; preserve existing installation.' }
        if ($checkout -ne $root) {
            & (Join-Path $checkout 'bootstrap/windows/install.ps1') -Action $Action -Harness $Harness -Server $Server -Connection $Connection -ExpectedConnectionSha256 $ExpectedConnectionSha256
            exit $LASTEXITCODE
        }
    }
    & $python -X utf8 -I -B (Join-Path $root 'src/agent_brain_client/interfaces/cli/install.py') @arguments
    exit $LASTEXITCODE
} finally {
    [Console]::OutputEncoding = $previousConsoleEncoding
    $OutputEncoding = $previousPipeEncoding
}

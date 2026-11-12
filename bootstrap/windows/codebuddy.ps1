[CmdletBinding()]
param(
    [string]$Server,
    [ValidateSet('install','reinstall','update')][string]$Action = 'install'
)
$ErrorActionPreference = 'Stop'
$Action = $Action.ToLowerInvariant()
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
. (Join-Path $PSScriptRoot 'python.ps1')
$python = Resolve-AgentBrainPython
$previousNativeHome = $env:CODEX_HOME
$previousConsoleEncoding = [Console]::OutputEncoding
$previousPipeEncoding = $OutputEncoding
$existingBackend = Test-Path (Join-Path $env:USERPROFILE '.config/agent-brain-client/codex-mcp.json')
try {
    [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
    $OutputEncoding = [Console]::OutputEncoding
    $env:CODEX_HOME = & $python -X utf8 -I -B (Join-Path $root 'bootstrap/codebuddy.py') backend-home
    if ($LASTEXITCODE -ne 0) { throw 'Existing shared Client binding could not be verified.' }
    & (Join-Path $PSScriptRoot 'install.ps1') -Action $Action -Harness codebuddy -Server $Server
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    if ($existingBackend -and $Action -eq 'install') {
        & (Join-Path $PSScriptRoot 'install.ps1') -Action check-update
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
        & (Join-Path $PSScriptRoot 'install.ps1') -Action update
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    }
    & $python -X utf8 -I -B (Join-Path $root 'bootstrap/codebuddy.py') install
    exit $LASTEXITCODE
} finally {
    $env:CODEX_HOME = $previousNativeHome
    [Console]::OutputEncoding = $previousConsoleEncoding
    $OutputEncoding = $previousPipeEncoding
}

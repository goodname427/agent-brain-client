[CmdletBinding()]
param(
    [string]$Server,
    [ValidateSet('install','update')][string]$Action = 'install'
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'windows-python.ps1')
$python = Resolve-AgentBrainPython
$previousNativeHome = $env:CODEX_HOME
$previousConsoleEncoding = [Console]::OutputEncoding
$previousPipeEncoding = $OutputEncoding
try {
    [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
    $OutputEncoding = [Console]::OutputEncoding
    $env:CODEX_HOME = & $python -X utf8 -I -B (Join-Path $root 'tools/codebuddy.py') backend-home
    if ($LASTEXITCODE -ne 0) { throw 'Existing shared Client binding could not be verified.' }
    & (Join-Path $PSScriptRoot 'install-windows.ps1') -Action $Action -Server $Server
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    & $python -X utf8 -I -B (Join-Path $root 'tools/codebuddy.py') install
    exit $LASTEXITCODE
} finally {
    $env:CODEX_HOME = $previousNativeHome
    [Console]::OutputEncoding = $previousConsoleEncoding
    $OutputEncoding = $previousPipeEncoding
}

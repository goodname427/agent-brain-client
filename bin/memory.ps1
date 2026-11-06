# Future-source management only; no downloads or secret parameters.
[CmdletBinding()]
param([Parameter(ValueFromRemainingArguments=$true)][string[]]$MemoryArgs)
$ErrorActionPreference = 'Stop'
$previousConsoleEncoding = [Console]::OutputEncoding
$previousPipeEncoding = $OutputEncoding
try {
    [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
    $OutputEncoding = [Console]::OutputEncoding
    if ($env:OS -ne 'Windows_NT') { throw 'Native Windows required.' }
    $root = Split-Path -Parent $PSScriptRoot
    $entry = if ($MemoryArgs.Count -gt 0 -and $MemoryArgs[0] -eq 'content-manage') { 'src/agent_brain_client/interfaces/management/server.py' } elseif ($MemoryArgs.Count -gt 0 -and $MemoryArgs[0].StartsWith('global-')) { 'src/agent_brain_client/interfaces/cli/collection.py' } else { 'src/agent_brain_client/interfaces/cli/project.py' }
    . (Join-Path $root 'bootstrap/windows/python.ps1')
    $python = Resolve-AgentBrainPython
    & $python -X utf8 -I -B (Join-Path $root $entry) @MemoryArgs
    exit $LASTEXITCODE
} finally {
    [Console]::OutputEncoding = $previousConsoleEncoding
    $OutputEncoding = $previousPipeEncoding
}

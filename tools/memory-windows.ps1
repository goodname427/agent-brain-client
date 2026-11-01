# Future-source management only; no downloads or secret parameters.
[CmdletBinding()]
param([Parameter(ValueFromRemainingArguments=$true)][string[]]$MemoryArgs)
$ErrorActionPreference = 'Stop'
if ($env:OS -ne 'Windows_NT') { throw 'Native Windows required.' }
$root = Split-Path -Parent $PSScriptRoot
$entry = if ($MemoryArgs.Count -gt 0 -and $MemoryArgs[0].StartsWith('global-')) { 'global_memory.py' } else { 'memory_automation.py' }
& python.exe -I -B (Join-Path (Join-Path $root 'tools') $entry) @MemoryArgs
exit $LASTEXITCODE

[CmdletBinding()]
param(
    [string]$Action = 'install',
    [string]$Surface,
    [string]$Server,
    [string]$AccessKeyFile,
    [string]$Connection,
    [string]$ExpectedConnectionSha256,
    [switch]$Help
)
$ErrorActionPreference = 'Stop'
& (Join-Path $PSScriptRoot 'install.ps1') -CodeBuddyEntry @PSBoundParameters
exit $LASTEXITCODE

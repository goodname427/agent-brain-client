# Native Windows entry. Uses normal per-user Python/Git and Codex hook review.
[CmdletBinding()]
param(
    [string]$Action = 'install',
    [string]$Harness,
    [string]$Surface,
    [string]$Server,
    [string]$AccessKeyFile,
    [string]$Connection,
    [string]$ExpectedConnectionSha256,
    [string]$UpdatePolicy,
    [string]$PauseUntil,
    [switch]$Help
)
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false
$Action = $Action.ToLowerInvariant()
$previousConsoleEncoding = [Console]::OutputEncoding
$previousPipeEncoding = $OutputEncoding
$stage = 'platform'

function Test-ClientStateAbsent {
    try {
        $config = Join-Path $env:USERPROFILE '.config'
        $rootItem = Get-Item -LiteralPath $config -Force -ErrorAction Stop
        if (-not $rootItem.PSIsContainer -or ($rootItem.Attributes -band [IO.FileAttributes]::ReparsePoint)) { return $false }
    } catch [System.Management.Automation.ItemNotFoundException] {
        return $true
    } catch {
        return $false
    }
    foreach ($name in @('agent-brain-client', 'agent-brain-reinstallation')) {
        try {
            $null = Get-Item -LiteralPath (Join-Path $config $name) -Force -ErrorAction Stop
            return $false
        } catch [System.Management.Automation.ItemNotFoundException] {
            continue
        } catch {
            return $false
        }
    }
    return $true
}

try {
    [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
    $OutputEncoding = [Console]::OutputEncoding
    if ($env:OS -ne 'Windows_NT') { throw 'Native Windows required; WSL uses a different OS and identity.' }
    $root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
    $stage = 'dependencies'
    . (Join-Path $PSScriptRoot 'python.ps1')
    $python = Resolve-AgentBrainPython
    $stage = 'arguments'
    $arguments = @('--action', $Action)
    if ($Help) { $arguments += '--help' }
    if ($Harness) { $arguments += @('--harness', $Harness.ToLowerInvariant()) }
    if ($Surface) { $arguments += @('--surface', $Surface.ToLowerInvariant()) }
    if ($Server) { $arguments += @('--server', $Server) }
    if ($AccessKeyFile) { $arguments += @('--access-key-file', $AccessKeyFile) }
    if ($Connection) { $arguments += @('--connection', $Connection) }
    if ($ExpectedConnectionSha256) { $arguments += @('--expected-connection-sha256', $ExpectedConnectionSha256) }
    if ($UpdatePolicy) { $arguments += @('--update-policy', $UpdatePolicy.ToLowerInvariant()) }
    if ($PauseUntil) { $arguments += @('--pause-until', $PauseUntil) }
    & $python -X utf8 -I -B (Join-Path $root 'bootstrap/source_checkout.py') --request-route @arguments
    $route = $LASTEXITCODE
    if ($route -eq 11) { exit 0 }
    if ($route -notin @(0, 10)) { exit $route }
    if ($route -eq 0) {
        $stage = 'checkout'
        $checkout = & $python -X utf8 -I -B (Join-Path $root 'bootstrap/source_checkout.py') --ensure
        if ($LASTEXITCODE -ne 0) { throw 'Fresh official checkout could not be verified; preserve existing installation.' }
        if ($checkout -ne $root) {
            & (Join-Path $checkout 'bootstrap/windows/install.ps1') @PSBoundParameters
            exit $LASTEXITCODE
        }
    }
    $stage = 'installer'
    & $python -X utf8 -I -B (Join-Path $root 'src/agent_brain_client/interfaces/cli/install.py') @arguments
    exit $LASTEXITCODE
} catch {
    if ($stage -eq 'dependencies' -and $Action -eq 'status' -and
        -not ($Help -or $Surface -or $Server -or $AccessKeyFile -or $Connection -or $ExpectedConnectionSha256 -or $UpdatePolicy -or $PauseUntil) -and
        (-not $Harness -or $Harness -in @('codex', 'codebuddy')) -and (Test-ClientStateAbsent)) {
        [ordered]@{
            operation = 'status'; outcome = 'completed'; status = 'client-not-installed'
            installation_state = 'absent'; installation_present = $false; installed = $false
            native_changed = $false; hosts = @{}; host_installations = @{}
        } | ConvertTo-Json -Compress
        exit 0
    }
    $status = switch ($stage) {
        'platform' { 'install-platform-review-required' }
        'dependencies' { 'install-dependencies-review-or-resume-required' }
        'checkout' { 'official-main-checkout-review-required' }
        default { 'installer-entry-review-required' }
    }
    [ordered]@{
        operation = $Action; outcome = 'review-required'; status = $status; installed = $false
        native_changed = $null; credential_accessed = $null; stage = $stage
    } | ConvertTo-Json -Compress
    exit 2
} finally {
    [Console]::OutputEncoding = $previousConsoleEncoding
    $OutputEncoding = $previousPipeEncoding
}

# Dependency selection only. Do not change PATH, registry, execution policy or install tools.
function Get-AgentBrainPythonCandidates {
    foreach ($name in @('python.exe', 'python3.exe', 'py.exe')) {
        foreach ($command in @(Get-Command $name -All -ErrorAction SilentlyContinue)) {
            if ($command.CommandType -ne 'Application') { continue }
            # App execution aliases can open the Store and are not pinnable executables.
            if ($command.Source -match '[\\/]Microsoft[\\/]WindowsApps[\\/]') { continue }
            if ($name -eq 'py.exe') {
                foreach ($version in @('-3', '-3.14', '-3.13', '-3.12', '-3.11')) {
                    @{ Command = $command.Source; Prefix = @($version) }
                }
            } else { @{ Command = $command.Source; Prefix = @() } }
        }
    }
    foreach ($registry in @('HKCU:\Software\Python\PythonCore', 'HKLM:\Software\Python\PythonCore', 'HKLM:\Software\WOW6432Node\Python\PythonCore')) {
        foreach ($version in @(Get-ChildItem $registry -ErrorAction SilentlyContinue | Sort-Object PSChildName -Descending)) {
            $install = Get-Item (Join-Path $version.PSPath 'InstallPath') -ErrorAction SilentlyContinue
            if ($install) {
                $executable = $install.GetValue('ExecutablePath')
                $directory = $install.GetValue('')
                if (-not $executable -and $directory) { $executable = Join-Path $directory 'python.exe' }
                if ($executable) { @{ Command = $executable; Prefix = @() } }
            }
        }
    }
    if ($env:LOCALAPPDATA) {
        foreach ($directory in @(Get-ChildItem (Join-Path $env:LOCALAPPDATA 'Programs/Python') -Directory -ErrorAction SilentlyContinue | Sort-Object Name -Descending)) {
            @{ Command = (Join-Path $directory.FullName 'python.exe'); Prefix = @() }
        }
    }
}

function Resolve-AgentBrainPython {
    param([object[]]$Candidates = @(Get-AgentBrainPythonCandidates))
    $probe = 'import json,os,ssl,sys,tomllib; from pathlib import Path; assert sys.platform == ''win32'' and os.name == ''nt'' and sys.version_info >= (3,11); p=Path(sys.executable).resolve(); assert p.is_absolute() and len(p.read_bytes()) > 0; assert ssl.create_default_context().get_ca_certs(); print(json.dumps(str(p)))'
    $seen = @{}
    foreach ($candidate in $Candidates) {
        $command = [string]$candidate.Command
        $prefix = @($candidate.Prefix)
        $identity = $command + '|' + ($prefix -join ' ')
        if ($seen.ContainsKey($identity) -or -not (Test-Path -LiteralPath $command -PathType Leaf)) { continue }
        if ($command -match '[\\/]Microsoft[\\/]WindowsApps[\\/]') { continue }
        $seen[$identity] = $true
        try {
            $output = & $command @prefix -X utf8 -I -B -c $probe 2>$null
            if ($LASTEXITCODE -ne 0) { continue }
            $python = ($output -join "`n") | ConvertFrom-Json
            if ($python -is [string] -and [IO.Path]::IsPathRooted($python) -and (Test-Path -LiteralPath $python -PathType Leaf)) {
                return $python
            }
        } catch { continue }
    }
    throw 'No usable Python 3.11+ with TLS roots and a readable executable was found. Install Python for this user, then rerun the same entry. See docs/FIRST-INSTALL-WINDOWS.md.'
}

#Requires -Version 5.1
<#
.SYNOPSIS
    Windows setup script for lsl-av-recorder. Mirrors setup.sh.

.DESCRIPTION
    Installs LabRecorder via GitHub, then creates a Python virtual environment
    and installs the package (which pulls in pygrabber for DirectShow-based
    camera enumeration/capability probing on Windows).

.NOTES
    Run from an elevated or regular PowerShell prompt in the repo root:
        .\setup.ps1
    If script execution is disabled, run once as Administrator:
        Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
#>

$ErrorActionPreference = "Stop"

function Test-CommandExists {
    param([string]$Name)
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

# --- liblsl ---
# pylsl (installed below via pip) bundles a precompiled liblsl for win_amd64, so no
# separate system install is required. If pylsl fails to locate liblsl at runtime,
# download a liblsl release manually from:
#   https://github.com/sccn/liblsl/releases
# and place liblsl.dll on PATH or next to main.py.

# LSL LabRecorder
$labRecorderDir = Join-Path $PSScriptRoot "LabRecorder"
if (Test-Path $labRecorderDir) {
    Write-Host "LabRecorder already present at $labRecorderDir"
} else {
    Write-Host "Fetching latest LabRecorder Windows release..."
    try {
        $release = Invoke-RestMethod -Uri "https://api.github.com/repos/labstreaminglayer/App-LabRecorder/releases/latest"
        $asset = $release.assets | Where-Object { $_.name -match "(?i)win" } | Select-Object -First 1
        if (-not $asset) {
            throw "No Windows asset found in latest release."
        }
        $zipPath = Join-Path $env:TEMP $asset.name
        Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $zipPath
        Expand-Archive -Path $zipPath -DestinationPath $labRecorderDir -Force
        Remove-Item $zipPath -Force
        $exe = Get-ChildItem -Path $labRecorderDir -Filter "LabRecorder.exe" -Recurse | Select-Object -First 1
        if ($exe) {
            Write-Host "LabRecorder installed. Launch with:"
            Write-Host "  & `"$($exe.FullName)`""
        } else {
            Write-Host "LabRecorder archive extracted to $labRecorderDir, but LabRecorder.exe was not found; check the archive contents."
        }
    } catch {
        Write-Warning "Could not automatically download LabRecorder ($_). Please download it manually from:"
        Write-Warning "  https://github.com/labstreaminglayer/App-LabRecorder/releases"
    }
}

# Install Python virtual environment
$python = if (Test-CommandExists "python") { "python" } elseif (Test-CommandExists "py") { "py" } else { $null }
if (-not $python) {
    throw "Python 3.10+ is required but was not found on PATH. Install it from https://www.python.org/downloads/."
}

& $python -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -U pip
& .\.venv\Scripts\python.exe -m pip install -e .

Write-Host ""
Write-Host "Setup complete. Activate the environment with:"
Write-Host "  .\.venv\Scripts\Activate.ps1"

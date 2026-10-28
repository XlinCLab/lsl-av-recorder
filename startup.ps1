#Requires -Version 5.1
<#
.SYNOPSIS
    Launch lsl-av-recorder from anywhere. Windows equivalent of startup.sh.

.DESCRIPTION
    The repo location is derived from this script's own path, so it does not
    matter where the repo is cloned or which directory this is run from.
    Any arguments are passed through to main.py.

.NOTES
    If script execution is disabled, run once:
        Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
    or launch it with:
        powershell -ExecutionPolicy Bypass -File <path>\startup.ps1
#>

$ErrorActionPreference = "Stop"

$repoDir = $PSScriptRoot
$python = Join-Path $repoDir ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    [Console]::Error.WriteLine("No virtual environment found at $(Join-Path $repoDir '.venv'). Run $(Join-Path $repoDir 'setup.ps1') first.")
    exit 1
}

# Relative paths in the app (e.g. the default ./recordings output folder) resolve
# against the working directory, so always run from the repo root.
Set-Location -LiteralPath $repoDir
& $python main.py @args
exit $LASTEXITCODE

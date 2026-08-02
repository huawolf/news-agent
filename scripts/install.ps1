# One-command installer for Windows PowerShell 5.1+ and PowerShell 7+.
[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$ProjectDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $ProjectDir

function Get-UvCommand {
    $command = Get-Command uv -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }

    $localBin = Join-Path $env:USERPROFILE ".local\bin"
    $cargoBin = Join-Path $env:USERPROFILE ".cargo\bin"

    foreach ($dir in @($localBin, $cargoBin)) {
        $exe = Join-Path $dir "uv.exe"
        if (Test-Path $exe) {
            $env:Path = "$dir;$env:Path"
            return $exe
        }
    }

    Write-Host "uv was not found. Installing uv for the current user..."
    Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression

    $env:Path = "$localBin;$cargoBin;$env:Path"
    $command = Get-Command uv -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }

    foreach ($dir in @($localBin, $cargoBin)) {
        $exe = Join-Path $dir "uv.exe"
        if (Test-Path $exe) { return $exe }
    }

    throw "uv installation completed, but uv executable was not found on PATH or standard install locations. Please restart PowerShell and rerun this script."
}

$Uv = Get-UvCommand

if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "Created .env from .env.example. Add your LLM and push credentials before the first fetch."
}

Write-Host "Syncing locked runtime dependencies..."
& $Uv sync --locked --no-dev

Write-Host "Installing the current-user startup task..."
& $Uv run --no-sync python -m src.main service install

Write-Host "Starting the local service..."
& $Uv run --no-sync python -m src.main service start

Write-Host "News Agent is ready at http://127.0.0.1:12301"

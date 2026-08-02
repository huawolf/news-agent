# One-command installer for Windows PowerShell 5.1+ and PowerShell 7+.
[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$ProjectDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $ProjectDir

function Get-UvCommand {
    $command = Get-Command uv -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }

    Write-Host "uv was not found. Installing it for the current user..."
    Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
    $command = Get-Command uv -ErrorAction SilentlyContinue
    if (-not $command) {
        throw "uv installation completed but uv is not on PATH. Open a new PowerShell window and rerun this script."
    }
    return $command.Source
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

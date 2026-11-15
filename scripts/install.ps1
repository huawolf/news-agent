# One-command installer for Windows PowerShell 5.1+ and PowerShell 7+.
[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

function Assert-NativeSuccess {
    param([string]$Operation)
    if ($LASTEXITCODE -ne 0) {
        throw "$Operation failed with exit code $LASTEXITCODE."
    }
}

if ((Test-Path "pyproject.toml") -and (Test-Path "src")) {
    $ProjectDir = (Get-Location).Path
} elseif ($PSScriptRoot -and (Test-Path (Join-Path $PSScriptRoot "..\pyproject.toml"))) {
    $ProjectDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
} else {
    $ProjectDir = Join-Path $env:USERPROFILE ".news-agent"
    Write-Host "Cloning news-agent into $ProjectDir..."
    if (Test-Path $ProjectDir) {
        Set-Location $ProjectDir
        git pull --rebase
        Assert-NativeSuccess "git pull"
    } else {
        git clone https://github.com/huawolf/news-agent.git $ProjectDir
        Assert-NativeSuccess "git clone"
    }
}
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

if (-not (Test-Path "config.json")) {
    Copy-Item "config.json.example" "config.json"
    Write-Host "Created config.json from config.json.example. Review mode, database, and API settings."
}

Write-Host "Syncing locked runtime dependencies..."
& $Uv sync --locked --no-dev
Assert-NativeSuccess "uv sync"

Write-Host "Installing the current-user startup task..."
& $Uv run --no-sync python -m src.main service install
Assert-NativeSuccess "service install"

Write-Host "Restarting the local service..."
& $Uv run --no-sync python -m src.main service restart
Assert-NativeSuccess "service restart"

Write-Host "Checking local service health..."
for ($attempt = 1; $attempt -le 30; $attempt++) {
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:12301/api/status" -TimeoutSec 2
        if ($response.StatusCode -eq 200) {
            Write-Host "News Agent is ready at http://127.0.0.1:12301"
            exit 0
        }
    } catch {
    }
    Start-Sleep -Seconds 1
}
Write-Host "News Agent failed health check. Service status:"
& $Uv run --no-sync python -m src.main service status
exit 1

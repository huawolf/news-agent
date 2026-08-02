# Remove the current-user Windows startup task without deleting user data.
[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$ProjectDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $ProjectDir

$Uv = (Get-Command uv -ErrorAction Stop).Source
& $Uv run --no-sync python -m src.main service uninstall
Write-Host "News Agent service removed. User configuration and news data were kept."

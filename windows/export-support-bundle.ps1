$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Destination = Join-Path $ProjectRoot ("support-" + (Get-Date -Format "yyyyMMdd-HHmmss") + ".zip")
$Files = @(
    (Join-Path $ProjectRoot "config.toml"),
    (Join-Path $ProjectRoot "logs"),
    (Join-Path $ProjectRoot "data\diagnostics")
) | Where-Object { Test-Path $_ }
Compress-Archive -Path $Files -DestinationPath $Destination -CompressionLevel Optimal
Write-Host "Support bundle created without secrets.env or Discord state: $Destination"

[CmdletBinding()]
param(
    [switch]$DryRun,
    [ValidateRange(0, 300)]
    [int]$StartDelaySeconds = 0
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

$SecretsPath = Join-Path $ProjectRoot "secrets.env"
if (-not (Test-Path $SecretsPath)) {
    throw "secrets.env is missing; run windows\install.ps1 first."
}
foreach ($Line in Get-Content $SecretsPath) {
    if ($Line -match '^\s*([^#=]+)=(.*)$') {
        [Environment]::SetEnvironmentVariable($Matches[1].Trim(), $Matches[2].Trim(), "Process")
    }
}

$Tesseract = @(
    "$env:ProgramFiles\Tesseract-OCR\tesseract.exe",
    "${env:ProgramFiles(x86)}\Tesseract-OCR\tesseract.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $Tesseract) {
    throw "Tesseract was not found; run windows\install.ps1 again."
}
$env:TESSERACT_CMD = $Tesseract

$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$Config = Join-Path $ProjectRoot "config.toml"
if ($StartDelaySeconds -gt 0) {
    Start-Sleep -Seconds $StartDelaySeconds
}
$Arguments = @("-m", "pg_rankings", "--config", $Config, "once")
if ($DryRun) {
    $Arguments += "--dry-run"
}
& $Python @Arguments
exit $LASTEXITCODE

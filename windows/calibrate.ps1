$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot
$Tesseract = @(
    "$env:ProgramFiles\Tesseract-OCR\tesseract.exe",
    "${env:ProgramFiles(x86)}\Tesseract-OCR\tesseract.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $Tesseract) {
    throw "Tesseract was not found; run windows\install.ps1 first."
}
$env:TESSERACT_CMD = $Tesseract
$Output = & (Join-Path $ProjectRoot ".venv\Scripts\python.exe") -m pg_rankings `
    --config (Join-Path $ProjectRoot "config.toml") calibrate
$ExitCode = $LASTEXITCODE
$Output | Write-Output
if ($ExitCode -eq 0) {
    $Prefix = "Calibration artifacts: "
    $ArtifactLine = @($Output | ForEach-Object { "$_" } | Where-Object {
        $_.StartsWith($Prefix, [StringComparison]::Ordinal)
    }) | Select-Object -Last 1
    if ($ArtifactLine) {
        $ArtifactDirectory = $ArtifactLine.Substring($Prefix.Length)
        Copy-Item (Join-Path $ArtifactDirectory "report.json") `
            (Join-Path $ProjectRoot "data\calibration-latest.json") -Force
    }
}
exit $ExitCode

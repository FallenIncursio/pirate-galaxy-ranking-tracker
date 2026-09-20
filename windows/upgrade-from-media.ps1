#Requires -Version 5.1

[CmdletBinding()]
param(
    [string]$DestinationRoot = "C:\PirateGalaxyRankings"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = [IO.Path]::GetFullPath($DestinationRoot)
$SourceRoot = Split-Path -Parent $PSScriptRoot
$LogPath = Join-Path $ProjectRoot "logs\upgrade.log"
$Identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$Principal = [Security.Principal.WindowsPrincipal]::new($Identity)
if (-not $Principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    $Arguments = @(
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-File", "`"$PSCommandPath`""
    )
    Start-Process powershell.exe -Verb RunAs -ArgumentList $Arguments
    exit 0
}

New-Item -ItemType Directory -Force -Path (Join-Path $ProjectRoot "logs") | Out-Null
try {
    $Items = @(
        "src",
        "tests",
        "windows",
        "docs",
        "RESTORE.CMD",
        "PROFILE-800.CMD",
        "UPGRADE.CMD",
        "VERIFY.CMD",
        "README.md",
        "CHANGELOG.md",
        "CONTRIBUTING.md",
        "LICENSE",
        "SECURITY.md",
        "config.example.toml",
        "secrets.example.env",
        "pyproject.toml",
        ".gitignore"
    )
    foreach ($Item in $Items) {
        Copy-Item (Join-Path $SourceRoot $Item) $ProjectRoot -Recurse -Force
    }

    $Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
    & $Python -m pip install --disable-pip-version-check $ProjectRoot
    if ($LASTEXITCODE -ne 0) {
        throw "pip install failed with exit code $LASTEXITCODE"
    }
    & $Python -m pg_rankings --config (Join-Path $ProjectRoot "config.toml") validate-config
    if ($LASTEXITCODE -ne 0) {
        throw "config validation failed with exit code $LASTEXITCODE"
    }

    & (Join-Path $ProjectRoot "windows\update-scheduled-task.ps1")
    $Task = Get-ScheduledTask -TaskName "Pirate Galaxy Rankings"
    if ($Task.State -ne "Running") {
        Start-ScheduledTask -TaskName "Pirate Galaxy Rankings"
        Start-Sleep -Seconds 2
        $Task = Get-ScheduledTask -TaskName "Pirate Galaxy Rankings"
    }
    if ($Task.State -ne "Running") {
        throw "the tracker task did not start after the upgrade"
    }
    $Message = @(
        "UPGRADE_OK",
        "Task: $($Task.State)",
        "Reset countdown, multi-webhook targets, state migration, and direct worker installed.",
        (Get-Date -Format o)
    ) -join "`r`n"
}
catch {
    $Message = @(
        "UPGRADE_FAILED",
        $_.Exception.Message,
        (Get-Date -Format o)
    ) -join "`r`n"
}

$Message | Set-Content -Path $LogPath -Encoding UTF8
Write-Host $Message
Start-Process notepad.exe -ArgumentList $LogPath
if ($Message.StartsWith("UPGRADE_FAILED")) {
    exit 1
}

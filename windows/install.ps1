#Requires -Version 5.1
#Requires -RunAsAdministrator

[CmdletBinding()]
param(
    [switch]$SkipDependencies,
    [switch]$SkipScheduledTask
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

function Resolve-Python {
    $LocalPython = Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"
    if (Test-Path $LocalPython) {
        return $LocalPython
    }
    $PythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($PythonCommand) {
        $Version = & $PythonCommand.Source -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
        if ([version]$Version -ge [version]"3.11") {
            return $PythonCommand.Source
        }
    }
    throw "Python 3.11 or newer was not found. Re-run without -SkipDependencies."
}

function Install-TesseractDirect {
    $Version = "5.5.0.20241111"
    $Installer = Join-Path $env:TEMP "tesseract-ocr-w64-setup-$Version.exe"
    $DownloadUrl = "https://github.com/tesseract-ocr/tesseract/releases/download/5.5.0/tesseract-ocr-w64-setup-$Version.exe"
    $ExpectedHash = "F3FC4236425B690C8BE756F35793F77394EE004BE0A6460A440C754D892F68BC"

    Invoke-WebRequest -Uri $DownloadUrl -OutFile $Installer -UseBasicParsing
    $ActualHash = (Get-FileHash -Algorithm SHA256 $Installer).Hash
    if ($ActualHash -ne $ExpectedHash) {
        Remove-Item $Installer -Force -ErrorAction SilentlyContinue
        throw "The downloaded Tesseract installer failed SHA-256 verification."
    }

    try {
        $Process = Start-Process -FilePath $Installer -ArgumentList "/S" -Wait -PassThru
        if ($Process.ExitCode -ne 0) {
            throw "Tesseract setup failed with exit code $($Process.ExitCode)."
        }
    }
    finally {
        Remove-Item $Installer -Force -ErrorAction SilentlyContinue
    }
}

if (-not $SkipDependencies) {
    $WinGet = Get-Command winget.exe -ErrorAction SilentlyContinue
    try {
        $Python = Resolve-Python
    }
    catch {
        if (-not $WinGet) {
            throw "Python 3.11 or newer is required when WinGet is unavailable."
        }
        & $WinGet.Source install --exact --id Python.Python.3.12 --scope user `
            --accept-package-agreements --accept-source-agreements --silent
        if ($LASTEXITCODE -ne 0) {
            throw "WinGet could not install Python (exit code $LASTEXITCODE)."
        }
        $Python = Resolve-Python
    }

    $TesseractCandidates = @(
        "$env:ProgramFiles\Tesseract-OCR\tesseract.exe",
        "${env:ProgramFiles(x86)}\Tesseract-OCR\tesseract.exe"
    )
    if (-not ($TesseractCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1)) {
        if ($WinGet) {
            & $WinGet.Source install --exact --id tesseract-ocr.tesseract --source winget `
                --accept-package-agreements --accept-source-agreements --silent
        }
        if (-not $WinGet -or $LASTEXITCODE -ne 0 -or -not (
                $TesseractCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
            )) {
            Write-Warning "WinGet could not install Tesseract; using the verified official installer."
            Install-TesseractDirect
        }
    }
    if (-not ($TesseractCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1)) {
        throw "Tesseract installation completed without creating tesseract.exe."
    }
}

$Python = Resolve-Python
$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $VenvPython)) {
    & $Python -m venv (Join-Path $ProjectRoot ".venv")
}
& $VenvPython -m pip install --disable-pip-version-check --upgrade pip
& $VenvPython -m pip install --disable-pip-version-check $ProjectRoot

$ConfigPath = Join-Path $ProjectRoot "config.toml"
if (-not (Test-Path $ConfigPath)) {
    Copy-Item (Join-Path $ProjectRoot "config.example.toml") $ConfigPath
}
New-Item -ItemType Directory -Force -Path (Join-Path $ProjectRoot "data") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $ProjectRoot "logs") | Out-Null

$SecretsPath = Join-Path $ProjectRoot "secrets.env"
if (-not (Test-Path $SecretsPath)) {
    $SecureWebhook = Read-Host "Discord-Webhook-URL (Eingabe wird verborgen)" -AsSecureString
    $Pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($SecureWebhook)
    try {
        $Webhook = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($Pointer)
        if ($Webhook -notmatch '^https://(canary\.|ptb\.)?discord(app)?\.com/api/webhooks/') {
            throw "The supplied URL is not a Discord webhook URL."
        }
        [IO.File]::WriteAllText($SecretsPath, "DISCORD_WEBHOOK_URL=$Webhook`r`n", [Text.UTF8Encoding]::new($false))
    }
    finally {
        if ($Pointer -ne [IntPtr]::Zero) {
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($Pointer)
        }
        $Webhook = $null
    }
}

$Identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
& icacls.exe $SecretsPath /inheritance:r /grant:r "${Identity}:(M)" | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "The webhook file permissions could not be restricted."
}

if (-not $SkipScheduledTask) {
    $TaskName = "Pirate Galaxy Rankings"
    $HiddenPython = Join-Path $ProjectRoot ".venv\Scripts\pythonw.exe"
    $Worker = Join-Path $PSScriptRoot "run-worker.py"
    $Action = New-ScheduledTaskAction -Execute $HiddenPython `
        -Argument "`"$Worker`"" -WorkingDirectory $ProjectRoot
    $Trigger = New-ScheduledTaskTrigger -AtLogOn -User $Identity
    $Principal = New-ScheduledTaskPrincipal -UserId $Identity -LogonType Interactive -RunLevel Limited
    $Settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger `
        -Principal $Principal -Settings $Settings -Force | Out-Null
    & (Join-Path $PSScriptRoot "install-watchdog.ps1")
}

& $VenvPython -m pg_rankings --config $ConfigPath validate-config
Write-Host "Installation abgeschlossen."
Write-Host "1. Pirate Galaxy starten und Division 1 geöffnet lassen."
Write-Host "2. windows\calibrate.ps1 ausführen."
Write-Host "3. Bei erfolgreicher Kalibrierung die geplante Aufgabe starten."

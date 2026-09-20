#Requires -Version 5.1

[CmdletBinding()]
param(
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$')]
    [string]$Target = "primary",
    [switch]$HexInput,
    [switch]$Restart
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$SecretsPath = Join-Path $ProjectRoot "secrets.env"
$ConfigPath = Join-Path $ProjectRoot "config.toml"
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    throw "The tracker environment is missing; run windows\install.ps1 first."
}
$EnvironmentName = (& $Python -m pg_rankings --config $ConfigPath `
    webhook-environment --target $Target | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or $EnvironmentName -notmatch '^[A-Za-z_][A-Za-z0-9_]*$') {
    throw "Discord target '$Target' could not be resolved from config.toml."
}

$Prompt = if ($HexInput) {
    "Discord-Webhook-URL fuer '$Target' als Hex (Eingabe wird verborgen)"
}
else {
    "Discord-Webhook-URL fuer '$Target' (Eingabe wird verborgen)"
}
$SecureInput = Read-Host $Prompt -AsSecureString
$Pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($SecureInput)
try {
    $InputValue = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($Pointer)
    if ($HexInput) {
        if ($InputValue -notmatch '^(?:[0-9A-Fa-f]{2})+$') {
            throw "The supplied hex input is invalid."
        }
        [byte[]]$Bytes = for ($Index = 0; $Index -lt $InputValue.Length; $Index += 2) {
            [Convert]::ToByte($InputValue.Substring($Index, 2), 16)
        }
        $Webhook = [Text.Encoding]::UTF8.GetString($Bytes)
    }
    else {
        $Webhook = $InputValue
    }
    if ($Webhook -notmatch '^https://(canary\.|ptb\.)?discord(app)?\.com/api/webhooks/[0-9]+/[A-Za-z0-9._-]+$') {
        throw "The supplied URL is not a valid Discord webhook URL."
    }
    if (-not $Webhook.IsNormalized() -or $Webhook -notmatch '^[\x00-\x7F]+$') {
        throw "The supplied URL contains unexpected characters."
    }
    $Lines = if (Test-Path $SecretsPath) {
        [IO.File]::ReadAllLines($SecretsPath)
    }
    else {
        @()
    }
    $UpdatedLines = [Collections.Generic.List[string]]::new()
    $Found = $false
    $TargetPattern = '^\s*' + [Regex]::Escape($EnvironmentName) + '\s*='
    foreach ($Line in $Lines) {
        if ($Line -match $TargetPattern) {
            if (-not $Found) {
                $UpdatedLines.Add("$EnvironmentName=$Webhook")
                $Found = $true
            }
        }
        else {
            $UpdatedLines.Add($Line)
        }
    }
    if (-not $Found) {
        $UpdatedLines.Add("$EnvironmentName=$Webhook")
    }
    $Body = ($UpdatedLines -join "`r`n") + "`r`n"
    [IO.File]::WriteAllText($SecretsPath, $Body, [Text.UTF8Encoding]::new($false))
}
finally {
    if ($Pointer -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($Pointer)
    }
    $Webhook = $null
    $InputValue = $null
    $Bytes = $null
    $Body = $null
    $Lines = $null
    $UpdatedLines = $null
}

$Identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
& icacls.exe $SecretsPath /inheritance:r /grant:r "${Identity}:(M)" | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "The webhook file permissions could not be restricted."
}
Write-Host "Webhook fuer Ziel '$Target' sicher gespeichert."

if ($Restart) {
    $TaskName = "Pirate Galaxy Rankings"
    $Task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
    if ($Task.State -eq "Running") {
        Stop-ScheduledTask -TaskName $TaskName
        $Deadline = (Get-Date).AddSeconds(15)
        do {
            Start-Sleep -Milliseconds 250
            $Task = Get-ScheduledTask -TaskName $TaskName
        } while ($Task.State -eq "Running" -and (Get-Date) -lt $Deadline)
        if ($Task.State -eq "Running") {
            throw "The tracker task did not stop within 15 seconds."
        }
    }
    Start-ScheduledTask -TaskName $TaskName
    Start-Sleep -Seconds 2
    $Task = Get-ScheduledTask -TaskName $TaskName
    if ($Task.State -ne "Running") {
        throw "The tracker task did not restart."
    }
    Write-Host "Geplante Aufgabe neu gestartet: $($Task.State)"
}

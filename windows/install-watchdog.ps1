#Requires -Version 5.1

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$TaskName = 'Pirate Galaxy Watchdog'
$Identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$HiddenPython = Join-Path $ProjectRoot '.venv\Scripts\pythonw.exe'
$Watchdog = Join-Path $PSScriptRoot 'watchdog.py'

$Action = New-ScheduledTaskAction `
    -Execute $HiddenPython `
    -Argument "`"$Watchdog`"" `
    -WorkingDirectory $ProjectRoot
$Trigger = New-ScheduledTaskTrigger `
    -Once `
    -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 5) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
$Principal = New-ScheduledTaskPrincipal `
    -UserId $Identity `
    -LogonType Interactive `
    -RunLevel Limited
$Settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 2) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Principal $Principal `
    -Settings $Settings `
    -Force | Out-Null

Write-Host "Geplante Aufgabe installiert: $TaskName"

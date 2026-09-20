#Requires -Version 5.1

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$TaskName = "Pirate Galaxy Rankings"
$HiddenPython = Join-Path $ProjectRoot ".venv\Scripts\pythonw.exe"
$Worker = Join-Path $PSScriptRoot "run-worker.py"
$Task = Get-ScheduledTask -TaskName $TaskName
$WasRunning = $Task.State -eq "Running"

$Action = New-ScheduledTaskAction -Execute $HiddenPython `
    -Argument "`"$Worker`"" -WorkingDirectory $ProjectRoot

if ($WasRunning) {
    Stop-ScheduledTask -TaskName $TaskName
}
$TrackerProcesses = Get-CimInstance Win32_Process | Where-Object {
    $_.Name -match '^pythonw?\.exe$' -and
    $_.CommandLine -match ([Regex]::Escape($ProjectRoot)) -and
    $_.CommandLine -match 'run-(hidden|worker)\.py'
}
foreach ($Process in $TrackerProcesses) {
    Stop-Process -Id $Process.ProcessId -Force -ErrorAction SilentlyContinue
}
if ($TrackerProcesses) {
    Start-Sleep -Milliseconds 500
}
try {
    Set-ScheduledTask -TaskName $TaskName -Action $Action | Out-Null
}
finally {
    if ($WasRunning) {
        Start-ScheduledTask -TaskName $TaskName
    }
}

$Updated = Get-ScheduledTask -TaskName $TaskName
if ($WasRunning -and $Updated.State -ne "Running") {
    throw "The tracker task did not restart."
}
Write-Host "Geplante Aufgabe aktualisiert: $($Updated.State)"

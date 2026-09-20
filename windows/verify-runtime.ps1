#Requires -Version 5.1

[CmdletBinding()]
param(
    [switch]$NoOpen
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$ReportPath = Join-Path $ProjectRoot "logs\verification.log"
$Lines = [Collections.Generic.List[string]]::new()
$Lines.Add("Pirate Galaxy Rankings runtime verification")
$Lines.Add((Get-Date -Format o))

$Task = Get-ScheduledTask -TaskName "Pirate Galaxy Rankings"
$Lines.Add("TaskState=$($Task.State)")
foreach ($Action in $Task.Actions) {
    $Lines.Add("TaskExecute=$($Action.Execute)")
    $Lines.Add("TaskArguments=$($Action.Arguments)")
}

$Processes = @(
    Get-CimInstance Win32_Process | Where-Object {
        $_.Name -match '^pythonw?\.exe$' -and
        $_.CommandLine -match ([Regex]::Escape($ProjectRoot)) -and
        $_.CommandLine -match 'run-(hidden|worker)\.py'
    }
)
$Lines.Add("TrackerProcessCount=$($Processes.Count)")
foreach ($Process in $Processes) {
    $Lines.Add("TrackerProcess=$($Process.ProcessId):$($Process.CommandLine)")
}

$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$ModulePath = (& $Python -c "import pg_rankings.discord_webhook as module; print(module.__file__)")
$Lines.Add("DiscordModule=$ModulePath")

$StatePath = Join-Path $ProjectRoot "data\state.json"
if (Test-Path $StatePath) {
    $State = Get-Content $StatePath -Raw | ConvertFrom-Json
    $Version = if ($State.version) { $State.version } else { 1 }
    $Lines.Add("StateVersion=$Version")
    if ($State.targets) {
        foreach ($Target in $State.targets.PSObject.Properties) {
            if ($Target.Value.messages) {
                foreach ($Message in $Target.Value.messages.PSObject.Properties) {
                    $HasMessage = -not [string]::IsNullOrWhiteSpace($Message.Value.message_id)
                    $Lines.Add(
                        "StateTarget=$($Target.Name);Card=$($Message.Name);MessageIdPresent=$HasMessage;Stale=$($Message.Value.stale_published)"
                    )
                }
            }
            else {
                $HasMessage = -not [string]::IsNullOrWhiteSpace($Target.Value.message_id)
                $Lines.Add(
                    "StateTarget=$($Target.Name);Card=ranking;MessageIdPresent=$HasMessage;Stale=$($Target.Value.stale_published)"
                )
            }
        }
    }
}
else {
    $Lines.Add("StateVersion=missing")
}

$Lines.Add("")
$Lines.Add("Latest tracker log:")
$TrackerLog = Join-Path $ProjectRoot "logs\tracker.log"
if (Test-Path $TrackerLog) {
    foreach ($Line in Get-Content $TrackerLog -Tail 30) {
        $Lines.Add($Line)
    }
}
$Lines.Add("")
$Lines.Add("Latest bootstrap log:")
$BootstrapLog = Join-Path $ProjectRoot "logs\bootstrap.log"
if (Test-Path $BootstrapLog) {
    foreach ($Line in Get-Content $BootstrapLog -Tail 20) {
        $Lines.Add($Line)
    }
}

$Lines | Set-Content -Path $ReportPath -Encoding UTF8
if ($NoOpen) {
    $Lines
}
else {
    Start-Process notepad.exe -ArgumentList $ReportPath
}

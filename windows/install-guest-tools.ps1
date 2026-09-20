#Requires -Version 5.1
#Requires -RunAsAdministrator

$ErrorActionPreference = "Stop"

$Wmic = Get-WindowsCapability -Online | Where-Object Name -Like "WMIC*" | Select-Object -First 1
if ($Wmic -and $Wmic.State -ne "Installed") {
    Write-Host "Installing the Windows WMIC optional capability required by Synology Guest Tool..."
    Add-WindowsCapability -Online -Name $Wmic.Name | Out-Null
}
elseif (-not $Wmic) {
    Write-Warning "This Windows image no longer offers WMIC. Continuing with Guest Tool setup; the tracker itself does not depend on WMIC or Guest Tool."
}

$Installer = Get-CimInstance Win32_LogicalDisk -Filter "DriveType = 5" |
    ForEach-Object { Join-Path "$($_.DeviceID)\" "Synology_VMM_Guest_Tool.msi" } |
    Where-Object { Test-Path $_ } |
    Select-Object -First 1

if (-not $Installer) {
    throw "Synology_VMM_Guest_Tool.msi was not found. Keep the Synology Guest Tool ISO mounted as the second virtual CD."
}

Write-Host "Installing Synology Guest Tool from $Installer..."
$Process = Start-Process -FilePath "msiexec.exe" `
    -ArgumentList "/i `"$Installer`" /passive /norestart" `
    -Wait -PassThru
if ($Process.ExitCode -notin @(0, 3010)) {
    throw "Synology Guest Tool setup failed with exit code $($Process.ExitCode)."
}

Write-Host "Synology Guest Tool installation completed. Restart Windows before continuing."

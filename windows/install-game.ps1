[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$DownloadUrl = "https://cdn.splitscreenstudios.com/splitscreen/games/pirategalaxy/PirateGalaxy_Setup.exe"
$Installer = Join-Path $env:TEMP "PirateGalaxy_Setup.exe"
Invoke-WebRequest -Uri $DownloadUrl -OutFile $Installer -UseBasicParsing
$Hash = (Get-FileHash -Algorithm SHA256 $Installer).Hash
Write-Host "Installer von der offiziellen Pirate-Galaxy-Domain geladen. SHA-256: $Hash"
Write-Host "Der folgende Installer bleibt interaktiv, damit Lizenztext und Zielpfad sichtbar sind."
Start-Process -FilePath $Installer -Wait

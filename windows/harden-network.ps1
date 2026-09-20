#Requires -Version 5.1
#Requires -RunAsAdministrator

[CmdletBinding(SupportsShouldProcess)]
param(
    [string[]]$PublicDns = @("1.1.1.1", "1.0.0.1")
)

$ErrorActionPreference = "Stop"
$Adapter = Get-NetAdapter | Where-Object Status -eq "Up" | Select-Object -First 1
if (-not $Adapter) {
    throw "No active network adapter was found."
}
if ($PSCmdlet.ShouldProcess($Adapter.Name, "set public DNS and block private-network egress")) {
    Set-DnsClientServerAddress -InterfaceIndex $Adapter.ifIndex -ServerAddresses $PublicDns
    Get-NetFirewallRule -DisplayName "PG Tracker - Block private networks" -ErrorAction SilentlyContinue | Remove-NetFirewallRule
    New-NetFirewallRule -DisplayName "PG Tracker - Block private networks" `
        -Direction Outbound -Action Block -Profile Any `
        -RemoteAddress @("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16") | Out-Null
    Set-NetFirewallProfile -Profile Domain, Private, Public -DefaultInboundAction Block
    Write-Host "Private-network egress is blocked. Internet, Pirate Galaxy and Discord remain reachable."
}

<#
Удаление службы коннектора и его правил брандмауэра. PowerShell от имени администратора:
    .\uninstall_service.ps1
Карты на контроллерах и данные в базе остаются.
#>
param([string]$ServiceName = "TurnstileConnector", [string]$Nssm = "")
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
if (-not $Nssm) {
    if (Test-Path "$root\tools\nssm.exe") { $Nssm = "$root\tools\nssm.exe" } else { $Nssm = "nssm" }
}
if (Get-Service $ServiceName -ErrorAction SilentlyContinue) {
    & $Nssm stop $ServiceName | Out-Null
    & $Nssm remove $ServiceName confirm | Out-Null
    Write-Host "Служба $ServiceName удалена"
}
Get-NetFirewallRule -DisplayName "TurnstileConnector *" -ErrorAction SilentlyContinue | Remove-NetFirewallRule
Write-Host "Правила брандмауэра удалены"

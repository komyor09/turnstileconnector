<#
Установка коннектора как службы Windows (через NSSM) и правил брандмауэра.

Запуск — PowerShell ОТ ИМЕНИ АДМИНИСТРАТОРА, из папки проекта:
    .\install_service.ps1 -IsuServerIp 192.168.100.100

Перед запуском:
  1. config.toml заполнен (скопирован из config.example.toml).
  2. nssm.exe лежит в .\tools\nssm.exe (скачать с nssm.cc, взять win64\nssm.exe) или доступен в PATH.
  3. Установлен uv (https://docs.astral.sh/uv/).

Повторный запуск безопасен: служба и правила брандмауэра пересоздаются.
#>
param(
    [Parameter(Mandatory = $true)][string]$IsuServerIp,       # кому разрешён вебхук (веб-сервер ISU)
    [string]$ServiceName    = "TurnstileConnector",
    [string]$Nssm           = "",
    [int]   $HttpPort       = 8090,
    [int]   $PushPort       = 60001,
    [string]$ControllersNet = "192.168.140.0/24"
)
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot

$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) { throw "Запустите PowerShell от имени администратора." }
if (-not (Test-Path "$root\config.toml")) { throw "Нет config.toml: скопируйте config.example.toml в config.toml и заполните." }

if (-not $Nssm) {
    if (Test-Path "$root\tools\nssm.exe") { $Nssm = "$root\tools\nssm.exe" }
    elseif (Get-Command nssm -ErrorAction SilentlyContinue) { $Nssm = (Get-Command nssm).Source }
    else { throw "Не найден nssm.exe: скачайте с nssm.cc и положите win64\nssm.exe в $root\tools\" }
}

Write-Host "1/4  Зависимости (Python ставится в папку проекта, служба не зависит от профиля пользователя)"
$env:UV_PYTHON_INSTALL_DIR = "$root\.python"
Push-Location $root
try {
    uv sync
    if ($LASTEXITCODE -ne 0) { throw "uv sync завершился с ошибкой" }
    $python = "$root\.venv\Scripts\python.exe"

    Write-Host "2/4  Проверка конфига, базы и контроллеров"
    & $python connector.py config.toml --check
    if ($LASTEXITCODE -ne 0) { throw "--check не прошёл: исправьте config.toml или сеть и запустите скрипт снова." }
} finally { Pop-Location }

Write-Host "3/4  Служба $ServiceName"
New-Item -ItemType Directory -Force "$root\logs" | Out-Null
if (Get-Service $ServiceName -ErrorAction SilentlyContinue) {
    & $Nssm stop $ServiceName | Out-Null
    & $Nssm remove $ServiceName confirm | Out-Null
}
& $Nssm install $ServiceName $python "connector.py config.toml" | Out-Null
& $Nssm set $ServiceName AppDirectory $root | Out-Null
& $Nssm set $ServiceName DisplayName "Turnstile Connector (ISU)" | Out-Null
& $Nssm set $ServiceName Description "Карты ISU -> турникеты, проходы -> база. Вебхук :$HttpPort/sync" | Out-Null
& $Nssm set $ServiceName Start SERVICE_AUTO_START | Out-Null
& $Nssm set $ServiceName AppEnvironmentExtra "PYTHONUNBUFFERED=1" "PYTHONIOENCODING=utf-8" | Out-Null
# Основной лог коннектор пишет сам (logs\connector.log, [log] в config.toml).
# Сюда попадает только то, что не прошло через логгер, например аварийный traceback.
& $Nssm set $ServiceName AppStdout "$root\logs\service-console.log" | Out-Null
& $Nssm set $ServiceName AppStderr "$root\logs\service-console.log" | Out-Null
& $Nssm set $ServiceName AppRotateFiles 1 | Out-Null
& $Nssm set $ServiceName AppRotateBytes 10485760 | Out-Null
& $Nssm set $ServiceName AppExit Default Restart | Out-Null
& $Nssm set $ServiceName AppRestartDelay 5000 | Out-Null

Write-Host "4/4  Брандмауэр: вебхук только с $IsuServerIp, push только из $ControllersNet"
Get-NetFirewallRule -DisplayName "TurnstileConnector *" -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule -DisplayName "TurnstileConnector webhook" -Direction Inbound -Protocol TCP `
    -LocalPort $HttpPort -RemoteAddress $IsuServerIp -Action Allow -Profile Any | Out-Null
New-NetFirewallRule -DisplayName "TurnstileConnector push" -Direction Inbound -Protocol UDP `
    -LocalPort $PushPort -RemoteAddress $ControllersNet -Action Allow -Profile Any | Out-Null

& $Nssm start $ServiceName | Out-Null
Start-Sleep -Seconds 3
Get-Service $ServiceName | Format-Table -AutoSize Name, Status, StartType
Write-Host "Лог: $root\logs\connector.log"

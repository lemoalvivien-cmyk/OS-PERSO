<#
  HERMES OS - Auto-start installer
  Creates a Windows scheduled task that runs GO.bat at user logon.
  Run once: .\install_autostart.ps1
  To remove: .\install_autostart.ps1 -Uninstall
#>
param([switch]$Uninstall)

$taskName = "HERMES_OS_Launcher"
$workDir = $PSScriptRoot
$batPath = Join-Path $workDir "GO.bat"

if ($Uninstall) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Auto-start removed." -ForegroundColor Yellow
    exit 0
}

if (-not (Test-Path $batPath)) {
    Write-Host "ERROR: GO.bat not found at $batPath" -ForegroundColor Red
    exit 1
}

# Create task that runs at logon, hidden, with highest priority
$action = New-ScheduledTaskAction -Execute $batPath -WorkingDirectory $workDir
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Days 0) `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1)

$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -RunLevel Limited -LogonType Interactive

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null

Write-Host ""
Write-Host "==========================================" -ForegroundColor Green
Write-Host "  HERMES OS - Auto-start INSTALLED" -ForegroundColor Green
Write-Host "==========================================" -ForegroundColor Green
Write-Host ""
Write-Host "  Task name:  $taskName" -ForegroundColor White
Write-Host "  Trigger:    Log on" -ForegroundColor Gray
Write-Host "  Script:     GO.bat (with watchdog)" -ForegroundColor Gray
Write-Host ""
Write-Host "  HERMES will start automatically at every login." -ForegroundColor Yellow
Write-Host "  To remove:  .\install_autostart.ps1 -Uninstall" -ForegroundColor DarkGray
Write-Host ""

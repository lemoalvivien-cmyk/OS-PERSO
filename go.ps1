<#
  HERMES OS - Launcher
  Usage: .\GO.ps1
  Ctrl+C to stop.
#>
param(
    [int]$Port = 9307,
    [string]$ApiKey = "",
    [int]$MaxRestarts = 10
)
$ErrorActionPreference = "Stop"

function W($I, $T) { Write-Host "  $I $T" }

Write-Host ""
Write-Host "==========================================" -ForegroundColor Cyan
Write-Host "  HERMES OS - LAUNCHER" -ForegroundColor Cyan
Write-Host "==========================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "[1/3] Python dependencies..." -ForegroundColor Yellow

$deps = @{
    "fastapi"    = "pip install fastapi"
    "uvicorn"    = "pip install uvicorn"
    "PIL"        = "pip install Pillow"
    "playwright" = "pip install playwright"
}
$ok = $true
foreach ($d in $deps.Keys) {
    try {
        python -c "import $d" 2>$null
        W "[OK]" $d
    } catch {
        W "[MISSING]" "$d - install: $($deps[$d])"
        $ok = $false
    }
}
if (-not $ok) {
    Write-Host "  Fix: pip install fastapi uvicorn Pillow playwright" -ForegroundColor Red
    $null = Read-Host "`nPress Enter when done"
}

Write-Host ""
Write-Host "[2/3] Infrastructure..." -ForegroundColor Yellow
foreach ($svc in @(
    @{N="Redis (6379)";  C="redis-cli ping"},
    @{N="Qdrant (6333)"; U="http://127.0.0.1:6333/collections"},
    @{N="Ollama (11434)";U="http://127.0.0.1:11434/api/tags"}
)) {
    try {
        if ($svc.C) { $r = Invoke-Expression $svc.C 2>$null; $hit = $r -match "PONG" }
        else { $null = Invoke-RestMethod $svc.U -TimeoutSec 3; $hit = $true }
        if ($hit) { W "[OK]" $svc.N } else { W "[--]" "$($svc.N) not running (ok)" }
    } catch { W "[--]" "$($svc.N) not running (ok)" }
}

Write-Host ""
Write-Host "[3/3] Starting HERMES..." -ForegroundColor Yellow

$mod = Join-Path $PSScriptRoot "hermes_computer_use.py"
if (-not (Test-Path $mod)) { Write-Host "  FATAL: hermes_computer_use.py missing" -ForegroundColor Red; exit 1 }
if ($ApiKey) { $env:HERMES_API_KEY = $ApiKey }

$p = $null; $rc = 0; $run = $true
try { [Console]::TreatControlCAsInput = $false } catch {}

$null = Register-EngineEvent -SourceIdentifier PowerShell.Exiting -Action {
    $script:run = $false
    if ($script:p -and -not $script:p.HasExited) { Stop-Process -Id $script:p.Id -Force -EA SilentlyContinue }
}

function SS {
    $script:p = Start-Process python -ArgumentList "-u",$mod,"--port",$Port -WorkingDirectory $PSScriptRoot -PassThru -WindowStyle Hidden
    $el = 0
    while ($el -lt 45) {
        Start-Sleep 2; $el += 2
        if ($script:p.HasExited) { W "[FAIL]" "Crash at startup"; return $false }
        try {
            $r = Invoke-RestMethod "http://127.0.0.1:$Port/health" -TimeoutSec 3
            if ($r.health -eq "ok") { W "[OK]" "Ready in ${el}s (PID:$($script:p.Id) port:$Port)"; return $true }
        } catch {}
    }
    W "[FAIL]" "Timeout"; return $false
}
function SP {
    if ($script:p -and -not $script:p.HasExited) { Stop-Process -Id $script:p.Id -Force -EA SilentlyContinue; Start-Sleep 2 }
}

Write-Host ""
if (SS) {
    Write-Host ""
    Write-Host "==========================================" -ForegroundColor Green
    Write-Host "  HERMES OS - ONLINE" -ForegroundColor Green
    Write-Host "==========================================" -ForegroundColor Green
    Write-Host ""
    Write-Host "  API:    http://127.0.0.1:$Port" -ForegroundColor White
    Write-Host "  Health: http://127.0.0.1:$Port/health" -ForegroundColor DarkGray
    Write-Host "  Docs:   http://127.0.0.1:$Port/docs" -ForegroundColor DarkGray
    Write-Host ""
    Write-Host "  Ctrl+C to stop | Watchdog: max $MaxRestarts restarts" -ForegroundColor DarkGray
    Write-Host ""

    while ($run -and $rc -lt $MaxRestarts) {
        Start-Sleep 30
        if ($script:p.HasExited) {
            $script:rc++; W "[$rc/$MaxRestarts]" "Crash - restarting..." -ForegroundColor Red
            SP; Start-Sleep 3; SS | Out-Null
        } else {
            try { $null = Invoke-RestMethod "http://127.0.0.1:$Port/health" -TimeoutSec 5 }
            catch {
                Start-Sleep 5
                try { $null = Invoke-RestMethod "http://127.0.0.1:$Port/health" -TimeoutSec 5 }
                catch {
                    $script:rc++; SP; Start-Sleep 3; SS | Out-Null
                }
            }
        }
    }
}
Write-Host "`n  Stopped." -ForegroundColor Gray; SP

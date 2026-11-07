# Stop ONLY the watchdog process recorded in state\watchdog.pid, after verifying its command line.
# Never touches Hermes, Strata or llama.cpp. (Stop-Process is a hard stop on Windows; all state files
# are written atomically, so nothing is corrupted.)
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$pidFile = Join-Path $Root 'state\watchdog.pid'
if (-not (Test-Path $pidFile)) { Write-Host 'no pid file: watchdog not running'; exit 0 }
$wdPid = [int](Get-Content $pidFile)
$proc = Get-Process -Id $wdPid -ErrorAction SilentlyContinue
if (-not $proc) { Remove-Item $pidFile -Force; Write-Host 'stale pid file removed'; exit 0 }
$cmd = (Get-CimInstance Win32_Process -Filter "ProcessId=$wdPid").CommandLine
if ($cmd -notmatch 'antiloop_watchdog') { Write-Error "pid $wdPid is not the watchdog; refusing to stop it"; exit 1 }
Stop-Process -Id $wdPid
$proc.WaitForExit(10000) | Out-Null
Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
Write-Host 'watchdog stopped'

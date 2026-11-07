# Start the watchdog hidden in the background (Windows). No global installs: uses .\.venv (stdlib only).
# Does NOT register a service or scheduled task.
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$py = Join-Path $Root '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) {
  if (Get-Command py -ErrorAction SilentlyContinue) { & py -3 -m venv .venv } else { & python -m venv .venv }
}
New-Item -ItemType Directory -Force -Path (Join-Path $Root 'state'), (Join-Path $Root 'logs') | Out-Null
$pidFile = Join-Path $Root 'state\watchdog.pid'
if (Test-Path $pidFile) {
  $old = [int](Get-Content $pidFile -ErrorAction SilentlyContinue)
  if ($old -and (Get-Process -Id $old -ErrorAction SilentlyContinue)) { Write-Host "watchdog already running (pid $old)"; exit 0 }
}
$null = Start-Process -FilePath $py -ArgumentList '-m','antiloop_watchdog' -WorkingDirectory $Root -WindowStyle Hidden `
  -RedirectStandardOutput (Join-Path $Root 'logs\stdout.log') -RedirectStandardError (Join-Path $Root 'logs\stderr.log')
$url = if ($env:WATCHDOG_URL) { $env:WATCHDOG_URL } else { 'http://127.0.0.1:8791' }
for ($i = 0; $i -lt 50; $i++) {
  & $py (Join-Path $Root 'tools\healthcheck.py') --url $url *> $null
  if ($LASTEXITCODE -eq 0) { Write-Host "watchdog is up at $url"; Write-Host "API token file: $Root\state\token"; exit 0 }
  Start-Sleep -Milliseconds 200
}
Write-Error "watchdog did not become healthy; see $Root\logs\stderr.log"; exit 1

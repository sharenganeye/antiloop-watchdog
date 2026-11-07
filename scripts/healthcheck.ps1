$Root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $Root '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) { $py = 'python' }
& $py (Join-Path $Root 'tools\healthcheck.py') @args
exit $LASTEXITCODE

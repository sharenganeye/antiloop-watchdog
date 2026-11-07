# Creates .\.venv if needed, installs the dev requirement (pytest) INSIDE it, runs the suite.
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$py = Join-Path $Root '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) { if (Get-Command py -ErrorAction SilentlyContinue) { & py -3 -m venv .venv } else { & python -m venv .venv } }
& $py -m pip install -q -r requirements-dev.txt
& $py -m pytest @args
exit $LASTEXITCODE

# Antiloop watchdog launcher - idempotent, no restart loops.
# Starts the shared (8791) and canary (8792) watchdogs only when their /health endpoint is not answering.
# Registered as a Windows logon task so both return after a reboot without any Hermes process being involved.

$ErrorActionPreference = 'SilentlyContinue'
$pythonw = 'C:\Users\zizor\Desktop\Antiloop\anti-loop-watchdog\.venv\Scripts\pythonw.exe'
$shared  = 'C:\Users\zizor\Desktop\Antiloop\anti-loop-watchdog'
$canary  = 'C:\Users\zizor\Desktop\Antiloop\anti-loop-watchdog-p1'
$log     = 'C:\Users\zizor\Desktop\Antiloop\watchdog_launcher.log'

function Test-Health([int]$port) {
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:$port/health" -TimeoutSec 3 -UseBasicParsing
        return ($r.StatusCode -eq 200 -and $r.Content -match '"status"\s*:\s*"ok"')
    } catch { return $false }
}

function Start-One([string]$label, [int]$port, [string]$workdir, [string[]]$extra) {
    if (Test-Health $port) {
        Add-Content $log ("{0} {1}:{2} already healthy - not starting" -f (Get-Date -Format s), $label, $port)
        return
    }
    $args = @('-m', 'antiloop_watchdog') + $extra
    Start-Process -FilePath $pythonw -ArgumentList $args -WorkingDirectory $workdir -WindowStyle Hidden
    Start-Sleep -Seconds 4
    $ok = Test-Health $port
    Add-Content $log ("{0} {1}:{2} started={3}" -f (Get-Date -Format s), $label, $port, $ok)
}

Start-One 'shared' 8791 $shared @()
Start-One 'canary' 8792 $canary @('--base-dir', $canary)

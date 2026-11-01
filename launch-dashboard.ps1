param([switch]$NoBrowser)

$ErrorActionPreference = 'Stop'
$taskRoot = $PSScriptRoot
$taskPython = Join-Path $taskRoot '.venv\Scripts\python.exe'
$dashboardUrl = 'http://127.0.0.1:8765'
$launchLogDirectory = Join-Path $taskRoot 'data\launcher'
$launchMutex = $null
$ownsLaunchMutex = $false

function Test-DashboardReady {
    try {
        $health = Invoke-RestMethod -Uri "$dashboardUrl/api/health" -TimeoutSec 2
        return ($health.status -eq 'ok' -and $health.pipeline_release -eq 'agentic_pipeline_v1')
    } catch {
        return $false
    }
}

try {
    # Serialize double-clicks, including while CLI preflight is running.
    $launchMutex = New-Object System.Threading.Mutex($false, 'Local\JobApplicationPipeline.Dashboard.8765')
    try { $ownsLaunchMutex = $launchMutex.WaitOne(0) }
    catch [System.Threading.AbandonedMutexException] { $ownsLaunchMutex = $true }
    if (!$ownsLaunchMutex) { exit 0 }

    if (!(Test-DashboardReady)) {
        if (!(Test-Path -LiteralPath $taskPython)) {
            throw 'The project Python environment is missing. See README.md to restore it.'
        }
        $listener = Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue
        if ($listener) {
            throw 'Port 8765 is already in use, but the pipeline dashboard is not responding. No existing process was stopped.'
        }

        New-Item -ItemType Directory -Path $launchLogDirectory -Force | Out-Null
        $launchStamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
        $preflightOut = Join-Path $launchLogDirectory "$launchStamp-preflight.stdout.log"
        $preflightErr = Join-Path $launchLogDirectory "$launchStamp-preflight.stderr.log"
        $preflight = Start-Process -FilePath $taskPython -ArgumentList '-m', 'app.cli_preflight' -WorkingDirectory $taskRoot -WindowStyle Hidden -RedirectStandardOutput $preflightOut -RedirectStandardError $preflightErr -PassThru -Wait
        if ($preflight.ExitCode -ne 0) {
            $preflightMessage = Get-Content -LiteralPath $preflightErr -Raw
            throw "Startup check failed. $preflightMessage`nLogs: $launchLogDirectory"
        }

        $serverOut = Join-Path $launchLogDirectory "$launchStamp-server.stdout.log"
        $serverErr = Join-Path $launchLogDirectory "$launchStamp-server.stderr.log"
        $server = Start-Process -FilePath $taskPython -ArgumentList '-m', 'uvicorn', 'app.server:create_app', '--factory', '--host', '127.0.0.1', '--port', '8765' -WorkingDirectory $taskRoot -WindowStyle Hidden -RedirectStandardOutput $serverOut -RedirectStandardError $serverErr -PassThru
        @{ pid=$server.Id; started_at=$server.StartTime.ToUniversalTime().ToString('o'); url=$dashboardUrl; executable=$taskPython } |
            ConvertTo-Json | Set-Content -LiteralPath (Join-Path $taskRoot 'data\server-process.json')

        $startupDeadline = [DateTime]::UtcNow.AddSeconds(45)
        while (!(Test-DashboardReady)) {
            if ([DateTime]::UtcNow -ge $startupDeadline) {
                throw "The dashboard did not become ready within 45 seconds. See $serverErr"
            }
            Start-Sleep -Milliseconds 400
        }
        Write-Output "Dashboard started: $dashboardUrl"
    } else {
        Write-Output "Dashboard already running: $dashboardUrl"
    }

    if (!$NoBrowser) { Start-Process $dashboardUrl }
} catch {
    $launchFailure = $_.Exception.Message
    Write-Output "Dashboard could not start: $launchFailure"
    if (!$NoBrowser) {
        $popup = New-Object -ComObject WScript.Shell
        $popup.Popup($launchFailure, 0, 'DDoJA - startup problem', 16) | Out-Null
    }
    exit 1
} finally {
    if ($ownsLaunchMutex) { $launchMutex.ReleaseMutex() }
    if ($launchMutex) { $launchMutex.Dispose() }
}

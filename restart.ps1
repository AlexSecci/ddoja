# Run manually when replacing a previously started background dashboard.
# Only the recorded, matching pipeline process tree can be stopped.
$ErrorActionPreference = 'Stop'
$taskRoot = $PSScriptRoot
. (Join-Path $taskRoot 'tools\server_process_identity.ps1')
$recordPath = Join-Path $taskRoot 'data\server-process.json'
$taskPython = Join-Path $taskRoot '.venv\Scripts\python.exe'
if (!(Test-Path -LiteralPath $taskPython)) { throw 'Project Python environment missing.' }
if (Test-Path -LiteralPath $recordPath) {
    $serverRecord = Get-Content -LiteralPath $recordPath | ConvertFrom-Json
    $serverParent = Get-CimInstance Win32_Process -Filter "ProcessId = $($serverRecord.pid)"
    if ($serverParent) {
        if ($serverParent.ExecutablePath -ne $taskPython -or
            [Math]::Abs(($serverParent.CreationDate.ToUniversalTime() - (ConvertTo-DashboardUtc $serverRecord.started_at)).TotalSeconds) -gt 15) {
            throw 'Saved PID no longer identifies this dashboard. No process was stopped.'
        }
        $serverChildren = @(Get-CimInstance Win32_Process -Filter "ParentProcessId = $($serverRecord.pid)")
        if ($serverChildren.Count -ne 1 -or $serverChildren[0].CommandLine -notmatch 'uvicorn app.server:create_app') {
            throw 'Unexpected process tree. No process was stopped.'
        }
        Stop-Process -Id $serverChildren[0].ProcessId
        Wait-Process -Id $serverChildren[0].ProcessId -Timeout 10 -ErrorAction SilentlyContinue
        Stop-Process -Id $serverParent.ProcessId -ErrorAction SilentlyContinue
    }
}
if (Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue) {
    throw 'Port 8765 is still occupied by an unverified process. No replacement server was started.'
}
$server = Start-Process -FilePath $taskPython -ArgumentList '-m','uvicorn','app.server:create_app','--factory','--host','127.0.0.1','--port','8765' -WorkingDirectory $taskRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $taskRoot 'data\server.stdout.log') -RedirectStandardError (Join-Path $taskRoot 'data\server.stderr.log') -PassThru
@{ pid=$server.Id; started_at=$server.StartTime.ToUniversalTime().ToString('o'); url='http://127.0.0.1:8765'; executable=$taskPython } | ConvertTo-Json | Set-Content -LiteralPath $recordPath
$startupDeadline = [DateTime]::UtcNow.AddSeconds(45)
do {
    try {
        $health = Invoke-RestMethod -Uri 'http://127.0.0.1:8765/api/health' -TimeoutSec 2
        if ($health.status -eq 'ok' -and $health.pipeline_release -eq 'agentic_pipeline_v1') { break }
    } catch { }
    if ([DateTime]::UtcNow -ge $startupDeadline) {
        throw 'Dashboard did not become ready within 45 seconds. See data\server.stderr.log.'
    }
    Start-Sleep -Milliseconds 400
} while ($true)
Write-Output 'Dashboard started. Refresh http://127.0.0.1:8765 in your browser.'

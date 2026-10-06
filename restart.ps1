# Run manually when replacing a previously started background dashboard.
# Only the recorded, matching pipeline process tree can be stopped.
$taskRoot = $PSScriptRoot
$recordPath = Join-Path $taskRoot 'data\server-process.json'
$taskPython = Join-Path $taskRoot '.venv\Scripts\python.exe'
if (!(Test-Path -LiteralPath $taskPython)) { throw 'Project Python environment missing.' }
if (Test-Path -LiteralPath $recordPath) {
    $serverRecord = Get-Content -LiteralPath $recordPath | ConvertFrom-Json
    $serverParent = Get-CimInstance Win32_Process -Filter "ProcessId = $($serverRecord.pid)"
    if ($serverParent) {
        if ($serverParent.ExecutablePath -ne $taskPython -or
            [Math]::Abs(($serverParent.CreationDate.ToUniversalTime() - [DateTime]::Parse($serverRecord.started_at).ToUniversalTime()).TotalSeconds) -gt 15) {
            throw 'Saved PID no longer identifies this dashboard. No process was stopped.'
        }
        $serverChildren = @(Get-CimInstance Win32_Process -Filter "ParentProcessId = $($serverRecord.pid)")
        if ($serverChildren.Count -ne 1 -or $serverChildren[0].CommandLine -notmatch 'uvicorn app.server:create_app') {
            throw 'Unexpected process tree. No process was stopped.'
        }
        Stop-Process -Id $serverChildren[0].ProcessId
        Stop-Process -Id $serverParent.ProcessId -ErrorAction SilentlyContinue
    }
}
$server = Start-Process -FilePath $taskPython -ArgumentList '-m','uvicorn','app.server:create_app','--factory','--host','127.0.0.1','--port','8765' -WorkingDirectory $taskRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $taskRoot 'data\server.stdout.log') -RedirectStandardError (Join-Path $taskRoot 'data\server.stderr.log') -PassThru
@{ pid=$server.Id; started_at=$server.StartTime.ToUniversalTime().ToString('o'); url='http://127.0.0.1:8765'; executable=$taskPython } | ConvertTo-Json | Set-Content -LiteralPath $recordPath
Write-Output 'Dashboard started. Refresh http://127.0.0.1:8765 in your browser.'

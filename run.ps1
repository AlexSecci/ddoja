param([switch]$Check, [switch]$PreflightOnly)
$taskRoot = $PSScriptRoot
$taskPython = Join-Path $taskRoot '.venv\Scripts\python.exe'
if (!(Test-Path -LiteralPath $taskPython)) { throw 'Project Python environment missing. See README.md.' }
Set-Location -LiteralPath $taskRoot
if ($Check) { & $taskPython -m pytest -q; exit $LASTEXITCODE }
& $taskPython -m app.cli_preflight
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
if ($PreflightOnly) { exit 0 }
& $taskPython -m uvicorn app.server:create_app --factory --host 127.0.0.1 --port 8765

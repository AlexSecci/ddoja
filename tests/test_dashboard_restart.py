"""Exercise the actual PowerShell conversion without starting/stopping processes."""
from pathlib import Path
import json
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_restart_process_identity_preserves_json_datetime_timezone_and_precision():
    shell = shutil.which("pwsh") or shutil.which("powershell")
    if not shell:
        pytest.skip("PowerShell is required for the Windows restart regression.")
    script = r'''
. ./tools/server_process_identity.ps1
$utc = [DateTime]::SpecifyKind([DateTime]::new(2026,10,27,19,27,24).AddTicks(3512730), [DateTimeKind]::Utc)
$record = @{ started_at=$utc.ToString('o') } | ConvertTo-Json | ConvertFrom-Json
$cases = @($record.started_at, $utc, $utc.ToLocalTime(), [DateTimeOffset]::new($utc),
           '2026-10-27T19:27:24.3512730Z', '2026-10-27T20:27:24.3512730+01:00')
$offsets = @($cases | ForEach-Object { ((ConvertTo-DashboardUtc $_) - $utc).Ticks })
$rejected = 0
foreach ($bad in @('2026-10-27T19:27:24', [DateTime]::new(2026,10,27,19,27,24))) {
    try { ConvertTo-DashboardUtc $bad | Out-Null } catch { $rejected++ }
}
@{ offsets=$offsets; rejected=$rejected } | ConvertTo-Json -Compress
'''
    result = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-Command", script], cwd=ROOT,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    values = json.loads(result.stdout)
    assert values["offsets"] == [0] * 6
    assert values["rejected"] == 2

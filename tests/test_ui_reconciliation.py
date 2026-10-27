from pathlib import Path
import shutil
import subprocess


def test_keyed_reconciliation_including_detached_toggle_and_ime():
    root = Path(__file__).resolve().parents[1]
    node = shutil.which("node")
    assert node, "The configured Node runtime is needed for the dashboard DOM regression."
    result = subprocess.run([node, str(root / "tests/ui_reconcile_harness.cjs"), str(root / "app/static/app.js")],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr

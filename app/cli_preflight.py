"""Safe startup checks for the local Codex CLI dependency."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable, Sequence

from .cli_paths import resolve_codex_cli


_TIMEOUT_SECONDS = 15


def check_codex_cli(*, resolver: Callable[[], str | None] = resolve_codex_cli,
                    runner: Callable = subprocess.run) -> tuple[bool, str]:
    """Check CLI availability and existing sign-in without starting a task."""
    codex = resolver()
    if not codex:
        return False, (
            "Codex CLI was not found for this Windows user. Install the official CLI, "
            "or set JOB_PIPELINE_CODEX_PATH to its executable, then run "
            ".\\run.ps1 -PreflightOnly. The server was not started."
        )

    checks: Sequence[tuple[list[str], str]] = (
        ([codex, "--version"], "The Codex CLI version check failed."),
        ([codex, "login", "status"], "Codex CLI is not signed in for this Windows user."),
    )
    for command, failure in checks:
        try:
            result = runner(command, capture_output=True, text=True,
                            timeout=_TIMEOUT_SECONDS, shell=False)
        except subprocess.TimeoutExpired:
            return False, (
                f"{failure} The command timed out. Check the existing Codex CLI installation "
                "and rerun .\\run.ps1 -PreflightOnly. The server was not started."
            )
        except OSError:
            return False, (
                f"{failure} Check the existing Codex CLI installation and "
                "JOB_PIPELINE_CODEX_PATH, then rerun .\\run.ps1 -PreflightOnly. "
                "The server was not started."
            )
        if result.returncode != 0:
            if command[-2:] == ["login", "status"]:
                powershell_executable = "'" + codex.replace("'", "''") + "'"
                return False, (
                    "Codex CLI was found but is not signed in for this Windows user. "
                    f"In PowerShell in this user session, run `& {powershell_executable} login`, "
                    f"then verify with `& {powershell_executable} login status`, "
                    "then rerun .\\run.ps1. This uses your existing ChatGPT account and does "
                    "not require an API key. The server was not started."
                )
            return False, (
                "Codex CLI was found but its version check failed. Repair the existing "
                "installation or set JOB_PIPELINE_CODEX_PATH to the intended executable, "
                "then rerun .\\run.ps1 -PreflightOnly. The server was not started."
            )

    return True, "Codex CLI version and existing login status checks passed."


def main() -> int:
    ok, message = check_codex_cli()
    output = sys.stdout if ok else sys.stderr
    print(message, file=output)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

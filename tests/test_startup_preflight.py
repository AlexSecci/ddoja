import subprocess

from app.cli_preflight import check_codex_cli


class FakeRunner:
    def __init__(self, returncodes):
        self.returncodes = iter(returncodes)
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, next(self.returncodes))


def test_missing_cli_is_actionable_and_does_not_run_commands():
    runner = FakeRunner([])
    ok, message = check_codex_cli(resolver=lambda: None, runner=runner)

    assert not ok
    assert "JOB_PIPELINE_CODEX_PATH" in message
    assert "server was not started" in message
    assert runner.calls == []


def test_preflight_checks_only_version_and_existing_login():
    runner = FakeRunner([0, 0])
    ok, message = check_codex_cli(resolver=lambda: r"C:\Codex\codex.exe", runner=runner)

    assert ok
    assert "version and existing login status checks passed" in message
    assert [call[0][1:] for call in runner.calls] == [["--version"], ["login", "status"]]
    assert all(call[1]["shell"] is False for call in runner.calls)
    assert all(call[1]["timeout"] == 15 for call in runner.calls)


def test_unsigned_cli_gets_existing_login_instructions():
    runner = FakeRunner([0, 1])
    codex = r"C:\Codex Folder\O'Neil\codex.exe"
    ok, message = check_codex_cli(resolver=lambda: codex, runner=runner)

    assert not ok
    assert "& 'C:\\Codex Folder\\O''Neil\\codex.exe' login" in message
    assert "login status" in message
    assert "server was not started" in message


def test_version_failure_stops_before_login_status():
    runner = FakeRunner([2])
    ok, message = check_codex_cli(resolver=lambda: "codex.exe", runner=runner)

    assert not ok
    assert "version check failed" in message
    assert len(runner.calls) == 1

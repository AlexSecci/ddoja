from __future__ import annotations

import json
import os
from pathlib import Path
import struct
from types import SimpleNamespace

import pytest

from app import agent_runtime, cli_paths, codex_runner, submission


def _write_pe(path: Path, machine: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    image = bytearray(512)
    image[:2] = b"MZ"
    struct.pack_into("<I", image, 0x3C, 0x80)
    image[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<H", image, 0x84, machine)
    path.write_bytes(image)
    return path


def test_resolver_preserves_explicit_override_and_prefers_path(monkeypatch, tmp_path):
    monkeypatch.setenv("JOB_PIPELINE_CODEX_PATH", "C:/configured/codex.exe")
    monkeypatch.setattr(cli_paths.shutil, "which", lambda _name: "C:/tools/codex.exe")
    monkeypatch.setattr(cli_paths.platform, "system", lambda: "Windows")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    assert cli_paths.resolve_codex_cli("codex-fixture") == "codex-fixture"
    assert cli_paths.resolve_codex_cli() == "C:/configured/codex.exe"
    monkeypatch.delenv("JOB_PIPELINE_CODEX_PATH")
    assert cli_paths.resolve_codex_cli() == "C:/tools/codex.exe"


def test_resolver_honors_task_specific_environment_override(monkeypatch, tmp_path):
    override = str(tmp_path / "configured-codex.exe")
    monkeypatch.setenv("JOB_PIPELINE_CODEX_PATH", override)
    monkeypatch.setattr(cli_paths.shutil, "which", lambda _name: "C:/tools/codex.exe")

    assert cli_paths.resolve_codex_cli() == override


def test_resolver_discovers_newest_valid_native_windows_install(monkeypatch, tmp_path):
    install_root = tmp_path / "OpenAI" / "Codex" / "bin"
    older = _write_pe(install_root / "older" / "codex.exe", 0x8664)
    newer = _write_pe(install_root / "newer" / "codex.exe", 0x8664)
    _write_pe(install_root / "wrong-architecture" / "codex.exe", 0xAA64)
    invalid = install_root / "invalid" / "codex.exe"
    invalid.parent.mkdir(parents=True)
    invalid.write_bytes(b"not a native executable" * 20)
    (install_root / "missing-cli").mkdir()
    (install_root / "missing-cli" / "rg.exe").write_bytes(b"rg")
    os.utime(older, ns=(1_000_000_000, 1_000_000_000))
    os.utime(newer, ns=(2_000_000_000, 2_000_000_000))

    monkeypatch.setattr(cli_paths.shutil, "which", lambda _name: None)
    monkeypatch.setattr(cli_paths.platform, "system", lambda: "Windows")
    monkeypatch.setattr(cli_paths.platform, "machine", lambda: "AMD64")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.delenv("JOB_PIPELINE_CODEX_PATH", raising=False)

    assert cli_paths.resolve_codex_cli() == str(newer)


def test_resolver_returns_none_when_path_and_windows_install_are_unavailable(monkeypatch, tmp_path):
    monkeypatch.setattr(cli_paths.shutil, "which", lambda _name: None)
    monkeypatch.setattr(cli_paths.platform, "system", lambda: "Windows")
    monkeypatch.setattr(cli_paths.platform, "machine", lambda: "AMD64")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.delenv("JOB_PIPELINE_CODEX_PATH", raising=False)
    install_root = tmp_path / "OpenAI" / "Codex" / "bin"
    (install_root / "only-tools").mkdir(parents=True)
    (install_root / "only-tools" / "rg.exe").write_bytes(b"tool")

    assert cli_paths.resolve_codex_cli() is None
    assert "JOB_PIPELINE_CODEX_PATH" in cli_paths.CODEX_CLI_NOT_FOUND


def test_all_worker_runners_use_shared_resolver(monkeypatch, tmp_path):
    resolved = str(tmp_path / "codex.exe")
    calls = []

    def resolver(explicit=None):
        calls.append(explicit)
        return explicit or resolved

    monkeypatch.setattr(agent_runtime, "resolve_codex_cli", resolver)
    monkeypatch.setattr(codex_runner, "resolve_codex_cli", resolver)
    monkeypatch.setattr(submission, "resolve_codex_cli", resolver)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "workers.json").write_text(
        json.dumps({"authentication": "chatgpt"}), encoding="utf-8")

    runtime = agent_runtime.CodexAgentRuntime(tmp_path)
    batch = codex_runner.CodexRunner(tmp_path)
    browser = submission.CodexBrowserRunner(tmp_path)
    assert runtime.codex == batch.codex == browser.codex == resolved
    assert calls == [None, None, None]


def test_runner_errors_explain_install_resolution_when_cli_is_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(agent_runtime, "resolve_codex_cli", lambda _explicit=None: None)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "workers.json").write_text(
        json.dumps({"authentication": "chatgpt"}), encoding="utf-8")

    with pytest.raises(agent_runtime.AgentRuntimeError, match="Codex desktop install"):
        agent_runtime.CodexAgentRuntime(tmp_path)


def test_codex_invocation_env_uses_unique_workspace_temp_without_mutating_parent(monkeypatch, tmp_path):
    monkeypatch.setattr(cli_paths.platform, "system", lambda: "Windows")
    base = {"PATH": "fixture-path", "TEMP": "user-temp", "TMP": "user-tmp"}

    first = cli_paths.codex_invocation_env(tmp_path, "invocation-one", base)
    second = cli_paths.codex_invocation_env(tmp_path, "invocation-two", base)

    first_temp = Path(first["TEMP"])
    second_temp = Path(second["TEMP"])
    assert first_temp == Path(first["TMP"]) and first_temp.is_dir()
    assert second_temp == Path(second["TMP"]) and second_temp.is_dir()
    assert first_temp.is_relative_to(tmp_path.resolve())
    assert second_temp.is_relative_to(tmp_path.resolve())
    assert first_temp != second_temp
    assert first["PATH"] == second["PATH"] == base["PATH"]
    assert base == {"PATH": "fixture-path", "TEMP": "user-temp", "TMP": "user-tmp"}


def test_codex_invocation_env_rejects_path_segments_and_is_a_copy_on_non_windows(monkeypatch, tmp_path):
    base = {"TEMP": "user-temp", "TMP": "user-tmp"}
    monkeypatch.setattr(cli_paths.platform, "system", lambda: "Windows")
    with pytest.raises(ValueError, match="host-generated"):
        cli_paths.codex_invocation_env(tmp_path, "..\\outside", base)

    monkeypatch.setattr(cli_paths.platform, "system", lambda: "Linux")
    returned = cli_paths.codex_invocation_env(tmp_path, "invocation", base)
    assert returned == base
    assert returned is not base
    assert base == {"TEMP": "user-temp", "TMP": "user-tmp"}


def test_browser_runner_passes_scoped_temp_to_popen_without_changing_parent_env(monkeypatch, tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "workers.json").write_text(json.dumps({
        "authentication": "chatgpt", "worker_timeout_seconds": 30,
        "models": {"submission": {"model": "gpt-6-luna", "effort": "medium"}},
    }), encoding="utf-8")
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("TEMP", "parent-temp")
    monkeypatch.setenv("TMP", "parent-tmp")
    monkeypatch.setattr(cli_paths.platform, "system", lambda: "Windows")
    launches = []

    class _Process:
        returncode = 0

        def __init__(self, _args, **kwargs):
            launches.append(kwargs["env"])

        def communicate(self, _prompt, timeout):
            assert timeout == 30
            events = [
                {"type": "thread.started", "thread_id": "fixture-session"},
                {"type": "item.completed", "item": {"type": "agent_message", "text": '{"status":"blocked"}'}},
                {"type": "turn.completed"},
            ]
            return "\n".join(json.dumps(event) for event in events), ""

    monkeypatch.setattr(submission.subprocess, "run", lambda *_args, **_kwargs:
                        SimpleNamespace(returncode=0, stdout="ChatGPT", stderr=""))
    monkeypatch.setattr(submission.subprocess, "Popen", _Process)

    result = submission.CodexBrowserRunner(tmp_path, "codex-fixture").run("fixture prompt")

    assert result["response"]["status"] == "blocked"
    assert len(launches) == 1
    child_env = launches[0]
    child_temp = Path(child_env["TEMP"])
    assert child_temp == Path(child_env["TMP"])
    assert child_temp.is_dir() and child_temp.is_relative_to(tmp_path.resolve())
    assert os.environ["TEMP"] == "parent-temp" and os.environ["TMP"] == "parent-tmp"


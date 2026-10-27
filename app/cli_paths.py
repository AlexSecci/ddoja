"""Locate the Codex CLI for workers launched outside the desktop app."""
from __future__ import annotations

import os
from pathlib import Path
import platform
import re
import shutil
import struct


def _windows_machine_preference() -> tuple[int, ...]:
    machine = platform.machine().casefold()
    if machine in {"amd64", "x86_64"}:
        return (0x8664, 0x014C)
    if machine in {"arm64", "aarch64"}:
        return (0xAA64, 0x8664, 0x014C)
    if machine in {"x86", "i386", "i686"}:
        return (0x014C,)
    return (0x8664, 0xAA64, 0x014C)


def _pe_machine(path: Path) -> int | None:
    """Return the PE machine type, rejecting truncated or non-PE files."""
    try:
        if not path.is_file() or path.stat().st_size < 256:
            return None
        with path.open("rb") as executable:
            dos = executable.read(64)
            if len(dos) < 64 or dos[:2] != b"MZ":
                return None
            pe_offset = struct.unpack_from("<I", dos, 0x3C)[0]
            if pe_offset < 64 or pe_offset > 16 * 1024 * 1024:
                return None
            executable.seek(pe_offset)
            header = executable.read(6)
        if len(header) != 6 or header[:4] != b"PE\0\0":
            return None
        return struct.unpack_from("<H", header, 4)[0]
    except (OSError, ValueError, struct.error):
        return None


def _installed_windows_cli() -> str | None:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        return None
    install_root = Path(local_app_data) / "OpenAI" / "Codex" / "bin"
    try:
        directories = list(install_root.iterdir())
    except OSError:
        return None

    preference = _windows_machine_preference()
    rank = {machine: index for index, machine in enumerate(preference)}
    candidates: list[tuple[int, int, str, Path]] = []
    for directory in directories:
        if not directory.is_dir():
            continue
        executable = directory / "codex.exe"
        pe_machine = _pe_machine(executable)
        if pe_machine not in rank:
            continue
        try:
            modified_ns = executable.stat().st_mtime_ns
        except OSError:
            continue
        # Prefer a native architecture, then the most recently installed valid
        # binary. The path is a stable tie-breaker when timestamps match.
        candidates.append((rank[pe_machine], -modified_ns,
                           str(executable).casefold(), executable))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[:3])
    return str(candidates[0][3])


def resolve_codex_cli(explicit=None) -> str | None:
    """Resolve Codex from explicit overrides, PATH, or its Windows app install.

    Explicit constructor overrides remain authoritative, including test wrappers
    that are not files on disk. The task-specific environment override is next;
    PATH remains the normal resolution path on every platform. Windows desktop
    installs are discovered only after PATH lookup.
    """
    if explicit is not None:
        return os.fspath(explicit)
    environment_override = os.environ.get("JOB_PIPELINE_CODEX_PATH")
    if environment_override:
        return environment_override
    from_path = shutil.which("codex") or shutil.which("codex.exe")
    if from_path:
        return from_path
    if platform.system().casefold() == "windows":
        return _installed_windows_cli()
    return None


def codex_invocation_env(root, invocation_id, base_env=None) -> dict[str, str]:
    """Return a private CLI environment with a sandbox-readable Windows temp.

    Codex's Windows read-only sandbox cannot read Node MCP kernels extracted to
    the user's default LocalAppData Temp directory. Put those ephemeral files
    below this workspace, whose inherited sandbox ACL permits read access. The
    caller supplies a host-generated invocation ID so parallel processes never
    share a temp directory. This function never mutates the parent environment.
    """
    environment = dict(os.environ if base_env is None else base_env)
    if platform.system().casefold() != "windows":
        return environment
    invocation_id = str(invocation_id)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", invocation_id):
        raise ValueError("Codex invocation ID must be a safe host-generated path segment.")
    workspace = Path(root).resolve()
    temp_dir = workspace / "data" / "agent-runtime" / invocation_id / "temp"
    resolved_temp = temp_dir.resolve()
    if not resolved_temp.is_relative_to(workspace):
        raise ValueError("Codex invocation temp directory must stay inside the workspace.")
    resolved_temp.mkdir(parents=True, exist_ok=True)
    environment["TEMP"] = str(resolved_temp)
    environment["TMP"] = str(resolved_temp)
    return environment


CODEX_CLI_NOT_FOUND = (
    "Codex CLI executable could not be found. Add `codex` to PATH or ensure the "
    "Codex desktop install is available under `%LOCALAPPDATA%\\OpenAI\\Codex\\bin`, "
    "or set `JOB_PIPELINE_CODEX_PATH` to the executable."
)

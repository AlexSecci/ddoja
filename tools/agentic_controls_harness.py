"""Isolated rendered-UI control harness for the agentic pipeline manager.

This opt-in harness serves the real FastAPI/static UI and real
``AgentPipelineManager`` using a temporary workspace. Its only runtime is a
barrier fake: it accepts discovery tasks for HTTPS ``.invalid`` board URLs,
marks them active, and waits until the real stop control asks it to stop. It
never opens a browser, performs discovery, calls a model, or creates jobs.

Serve only after the operator has reviewed this entry point and cleared
127.0.0.1:8765. In the rendered UI create the two printed companies, confirm
both discovery workers are active, stop Active A, verify Active B remains
active, then stop Active B. Workspace files are deliberately retained for
explicit operator cleanup; this script never recursively deletes a tree.

Run from the repository root with:
    python -m tools.agentic_controls_harness --self-check
    python -m tools.agentic_controls_harness --serve
"""

from __future__ import annotations

import argparse
import socket
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from app.server import ROOT, create_app
from app.store import Store
from app.pipelines import AgentPipelineManager


HOST = "127.0.0.1"
PORT = 8765
MARKER = "UI CONTROL TEST ONLY — fake discovery barrier; no model/browser/employer access."
WAIT_SECONDS = 12


class FakeBarrierRuntime:
    """Test runtime that holds only fixture discovery workers until stopped."""

    def __init__(self, max_wait_seconds: int = 600):
        self.max_wait_seconds = max_wait_seconds
        self.started: dict[str, threading.Event] = {}
        self.finished: dict[str, threading.Event] = {}
        self._lock = threading.Lock()
        self.calls: list[tuple[str, str]] = []

    def run(self, envelope, on_event, should_stop):
        task = envelope.get("task", {}) if isinstance(envelope, dict) else {}
        worker = task.get("worker", {}) if isinstance(task, dict) else {}
        pipeline = task.get("pipeline", {}) if isinstance(task, dict) else {}
        role = worker.get("role")
        pipeline_id = str(pipeline.get("id") or "")
        company_name = str(pipeline.get("company_name") or "")
        careers_url = str(pipeline.get("careers_url") or "")
        parsed = urlsplit(careers_url)
        if role != "discovery":
            raise RuntimeError("The control harness permits only discovery workers.")
        if not pipeline_id or parsed.scheme != "https" or not parsed.hostname or not parsed.hostname.endswith(".invalid"):
            raise RuntimeError("The control harness accepts only HTTPS .invalid fixture boards.")
        if "UI CONTROL TEST ONLY" not in company_name:
            raise RuntimeError("The control harness refuses companies without its fixture marker.")

        with self._lock:
            self.started.setdefault(pipeline_id, threading.Event()).set()
            self.finished.setdefault(pipeline_id, threading.Event())
            self.calls.append((pipeline_id, role))
        deadline = time.monotonic() + self.max_wait_seconds
        while time.monotonic() < deadline:
            if should_stop():
                with self._lock:
                    self.finished[pipeline_id].set()
                return {
                    "disposition": "interrupted",
                    "session_id": f"ui-control-test-{pipeline_id[:12]}",
                    "checkpoint": "ui_control_stop_barrier",
                }
            time.sleep(0.05)
        with self._lock:
            self.finished[pipeline_id].set()
        return {
            "disposition": "interrupted",
            "session_id": f"ui-control-test-{pipeline_id[:12]}",
            "checkpoint": "ui_control_barrier_timeout",
            "error": "The bounded UI control harness expired before a stop request.",
        }


def _new_workspace() -> Path:
    root = Path(tempfile.mkdtemp(prefix="agentic-ui-control-test-only-")).resolve()
    project_root = ROOT.resolve()
    temp_root = Path(tempfile.gettempdir()).resolve()
    if temp_root not in root.parents or project_root in root.parents or root == project_root:
        raise RuntimeError("Refusing a control-harness workspace outside the isolated system temp directory.")
    (root / "UI_CONTROL_TEST_ONLY.txt").write_text(MARKER + "\n", encoding="utf-8")
    return root


def _wait_until(predicate, timeout: float = WAIT_SECONDS) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return bool(predicate())


def self_check() -> None:
    """Exercise the real manager's stop isolation without starting a server."""
    root = _new_workspace()
    runtime = FakeBarrierRuntime(max_wait_seconds=30)
    app = create_app(root, runtime_factory=runtime)
    manager = app.state.agent_pipeline_manager
    store: Store = app.state.store
    if not isinstance(manager, AgentPipelineManager):
        raise AssertionError("The harness must instantiate the production AgentPipelineManager.")

    pipelines = []
    for key in ("active-a", "active-b"):
        pipeline = store.create_agent_pipeline(
            f"UI CONTROL TEST ONLY — {key}",
            f"https://{key}.ui-control-test.invalid/careers",
            {"title_contains": "Software Engineer", "location_contains": ""},
            company_id=f"ui-control-test-{key}",
        )
        pipelines.append(pipeline)
        manager.start(pipeline["id"])

    try:
        ids = [pipeline["id"] for pipeline in pipelines]
        if not all(_wait_until(lambda pipeline_id=pipeline_id: runtime.started.get(pipeline_id, threading.Event()).is_set())
                   for pipeline_id in ids):
            raise AssertionError("Both fake discovery workers did not reach their active barrier.")
        manager.stop(ids[0])
        if not _wait_until(lambda: store.get_agent_worker(store.list_agent_workers(ids[0])[0]["id"])["state"] == "stopped"):
            raise AssertionError("Active A did not stop at the fake runtime barrier.")
        worker_b = store.list_agent_workers(ids[1])[0]
        if worker_b["state"] != "running" or not worker_b.get("turn_active"):
            raise AssertionError("Stopping Active A incorrectly stopped or released Active B.")
        manager.stop(ids[1])
        if not _wait_until(lambda: store.get_agent_worker(worker_b["id"])["state"] == "stopped"):
            raise AssertionError("Active B did not stop at its own barrier.")
        print("UI CONTROL TEST ONLY self-check passed: real manager stopped A while B remained running.")
        print(f"Temporary workspace retained at: {root}")
    finally:
        manager.shutdown()
        app.state.pipeline_manager.shutdown()
        app.state.browser_check_pool.shutdown(wait=True, cancel_futures=True)


def serve() -> None:
    try:
        with socket.create_connection((HOST, PORT), timeout=0.3):
            raise RuntimeError(f"Port {PORT} is already in use; refusing to use another port.")
    except OSError:
        pass

    root = _new_workspace()
    runtime = FakeBarrierRuntime()
    app = create_app(root, runtime_factory=runtime)
    if not isinstance(app.state.agent_pipeline_manager, AgentPipelineManager):
        raise RuntimeError("The real AgentPipelineManager is required for this control harness.")
    print(MARKER)
    print(f"Temporary workspace retained at: {root}")
    print("External discovery, evaluation, submission, and document generation are disabled by the fake runtime.")
    print("Create exactly these companies in the rendered UI:")
    print("  Company: UI CONTROL TEST ONLY — Active A")
    print("  Careers URL: https://active-a.ui-control-test.invalid/careers")
    print("  Company: UI CONTROL TEST ONLY — Active B")
    print("  Careers URL: https://active-b.ui-control-test.invalid/careers")
    print("  Optional title: Software Engineer; leave location blank.")
    print("Expected: both discovery workers running; stop A; confirm B still running; stop B.")
    print(f"Open http://{HOST}:{PORT}; stop this server from its owning terminal when the control check ends.")
    import uvicorn

    uvicorn.run(app, host=HOST, port=PORT, log_level="warning", access_log=False)


def main() -> None:
    parser = argparse.ArgumentParser(description="Isolated agentic UI stop-isolation harness.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--self-check", action="store_true", help="Exercise real manager barriers in a temporary store; no listener/browser.")
    group.add_argument("--serve", action="store_true", help="Serve the rendered UI against a disposable fake-runtime workspace on 127.0.0.1:8765.")
    args = parser.parse_args()
    if args.self_check:
        self_check()
    else:
        serve()


if __name__ == "__main__":
    main()

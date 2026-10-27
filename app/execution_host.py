"""Lazy service-owned runtime lifecycle; no inference is run by health requests."""
from __future__ import annotations

import json
from pathlib import Path
import threading


class HybridRuntime:
    """Persistent document turns; browser and legacy CLI sessions retain exec.

    A qualified saved session binds the transport. A failed turn is never retried
    on another host, even if the original host loses its registry or connection.
    """
    def __init__(self, root, store, *, exec_factory=None, app_server_factory=None):
        self.root = root
        self.store = store
        self.exec_factory = exec_factory
        self.app_server_factory = app_server_factory
        self._runtimes = {}
        self._lock = threading.RLock()
        self._closed = False

    def _get(self, mode):
        with self._lock:
            if self._closed:
                raise RuntimeError("The execution host is shutting down.")
            if mode not in self._runtimes:
                if mode == "app_server":
                    if self.app_server_factory:
                        runtime = self.app_server_factory(self.root, store=self.store)
                    else:
                        from .app_server_runtime import CodexAppServerRuntime
                        runtime = CodexAppServerRuntime(self.root, store=self.store)
                elif self.exec_factory:
                    runtime = self.exec_factory(self.root)
                else:
                    from .agent_runtime import CodexAgentRuntime
                    runtime = CodexAgentRuntime(self.root)
                self._runtimes[mode] = runtime
            return self._runtimes[mode]

    def run(self, task, on_event, should_stop):
        envelope = task.get("task", task)
        worker = envelope.get("worker") or {}
        session = worker.get("session_id")
        if session:
            mode = "app_server" if str(session).startswith("app_server:") else "exec"
        else:
            mode = "app_server" if worker.get("role") == "document" else "exec"
        return self._get(mode).run(task, on_event, should_stop)

    def status(self):
        with self._lock:
            modes = {}
            for mode, runtime in self._runtimes.items():
                modes[mode] = runtime.status() if callable(getattr(runtime, "status", None)) else {
                    "mode": mode, "status": "initialized", "ready": None}
            return {"mode": "hybrid", "status": "closed" if self._closed else "initialized" if modes else "idle",
                    "ready": None, "hosts": modes,
                    "detail": "New document tasks use app-server; browser tasks and saved CLI sessions use exec."}

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            runtimes = list(self._runtimes.values())
        for runtime in runtimes:
            if callable(getattr(runtime, "close", None)):
                runtime.close()


class ExecutionHost:
    def __init__(self, root, *, factory=None):
        self.root = Path(root).resolve()
        self._factory = factory
        self._runtime = None
        self._closed = False
        self._lock = threading.RLock()
        config = self.root / "config/workers.json"
        settings = json.loads(config.read_text(encoding="utf-8")) if config.is_file() else {}
        self.mode = settings.get("execution_host", "exec")
        if self.mode not in {"exec", "app_server", "hybrid"}:
            raise ValueError("execution_host must be exec, hybrid or app_server.")

    def get(self, store):
        with self._lock:
            if self._closed:
                raise RuntimeError("The execution host is shutting down.")
            if self._runtime is None:
                if self._factory is not None:
                    self._runtime = self._factory(store)
                elif self.mode == "hybrid":
                    self._runtime = HybridRuntime(self.root, store)
                elif self.mode == "app_server":
                    from .app_server_runtime import CodexAppServerRuntime
                    self._runtime = CodexAppServerRuntime(self.root, store=store)
                else:
                    from .agent_runtime import CodexAgentRuntime
                    self._runtime = CodexAgentRuntime(self.root)
            return self._runtime

    def status(self):
        with self._lock:
            if self._closed:
                return {"mode": self.mode, "status": "closed"}
            if self._runtime is not None and callable(getattr(self._runtime, "status", None)):
                return self._runtime.status()
            return {"mode": self.mode, "status": "idle" if self._runtime is None else "initialized",
                    "ready": None,
                    "detail": ("No worker host has been started."
                               if self._runtime is None else
                               "CLI execution is initialized; connector availability is checked by each worker.")}

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            runtime = self._runtime
        if runtime is not None and callable(getattr(runtime, "close", None)):
            runtime.close()

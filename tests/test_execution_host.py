from concurrent.futures import ThreadPoolExecutor
import json

import pytest

from app.execution_host import ExecutionHost, HybridRuntime


def test_lazy_host_does_not_run_health_probes_and_is_one_instance_across_threads(tmp_path):
    calls = []

    class Runtime:
        def close(self):
            calls.append("close")

    def factory(store):
        calls.append(store)
        return Runtime()

    host = ExecutionHost(tmp_path, factory=factory)
    assert host.status()["status"] == "idle" and calls == []
    with ThreadPoolExecutor(max_workers=4) as pool:
        runtimes = list(pool.map(host.get, ["isolated-store"] * 8))
    assert all(runtime is runtimes[0] for runtime in runtimes)
    assert calls == ["isolated-store"]
    host.close()
    host.close()
    assert calls == ["isolated-store", "close"]
    assert host.status()["status"] == "closed"
    with pytest.raises(RuntimeError):
        host.get("isolated-store")


def test_unrecognized_host_configuration_fails_before_worker_start(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config/workers.json").write_text(json.dumps({"execution_host": "unknown"}))
    with pytest.raises(ValueError):
        ExecutionHost(tmp_path)


def test_hybrid_binds_saved_sessions_and_never_falls_back_after_error(tmp_path):
    calls = []

    class Runtime:
        def __init__(self, mode):
            self.mode = mode

        def run(self, task, *_):
            calls.append(self.mode)
            if task.get("fail"):
                raise RuntimeError("Host lost after starting a turn")
            return self.mode

    runtime = HybridRuntime(tmp_path, object(), exec_factory=lambda _: Runtime("exec"),
                            app_server_factory=lambda _, store: Runtime("app_server"))
    assert runtime.status()["status"] == "idle" and calls == []
    for role in ("discovery", "evaluator", "submission"):
        assert runtime.run({"task": {"worker": {"role": role}}}, None, None) == "exec"
    assert runtime.run({"task": {"worker": {"role": "document"}}}, None, None) == "app_server"
    assert runtime.run({"worker": {"role": "document", "session_id": "old-cli-session"}}, None, None) == "exec"
    saved = {"worker": {"role": "document", "session_id": "app_server:saved-thread"}, "fail": True}
    before = len(calls)
    with pytest.raises(RuntimeError, match="Host lost"):
        runtime.run(saved, None, None)
    assert calls[before:] == ["app_server"]

from concurrent.futures import ThreadPoolExecutor
import io
import json
import queue
import threading

from app.app_server_runtime import CodexAppServerRuntime


class Lines:
    def __init__(self):
        self.queue = queue.Queue()

    def put(self, value):
        self.queue.put(json.dumps(value) + "\n" if value is not None else None)

    def __iter__(self):
        while True:
            value = self.queue.get()
            if value is None:
                return
            yield value


class ProtocolProcess:
    instances = []

    def __init__(self, args, **kwargs):
        self.args, self.env = args, kwargs["env"]
        self.stdout, self.stderr = Lines(), io.StringIO("")
        self.stdin = self
        self.returncode = None
        self.methods, self.replies, self.threads = [], [], 0
        self.requests = []
        self.instances.append(self)

    def write(self, line):
        value = json.loads(line)
        method, params, identity = value.get("method"), value.get("params", {}), value.get("id")
        if method:
            self.methods.append(method)
            self.requests.append((method, params))
        if method == "initialize":
            self.stdout.put({"id": identity, "result": {}})
        elif method == "thread/start":
            self.threads += 1
            assert params["sandbox"] == "read-only"
            assert all(tool["type"] == "function" for tool in params["dynamicTools"][0]["tools"])
            self.stdout.put({"id": identity, "result": {"thread": {"id": f"thread-{self.threads}"}}})
        elif method == "thread/resume":
            self.stdout.put({"id": identity, "result": {"thread": {"id": params["threadId"]}}})
        elif method == "turn/start":
            thread = params["threadId"]
            turn = "turn-" + str(identity)
            self.stdout.put({"id": identity, "result": {"turn": {"id": turn}}})
            self.stdout.put({"method": "item/tool/call", "id": f"tool:{thread}:{turn}", "params": {
                "threadId": thread, "turnId": turn, "tool": "agent_bridge.get_context", "arguments": {}}})
        elif method == "turn/interrupt":
            self.stdout.put({"id": identity, "result": {}})
        elif not method and isinstance(identity, str) and identity.startswith("tool:"):
            self.replies.append(value)
            _, thread, turn = identity.split(":")
            self.stdout.put({"method": "item/completed", "params": {"threadId": thread, "turnId": turn, "item": {
                "type": "agentMessage", "text": json.dumps({"disposition": "completed", "checkpoint": "fixture", "summary": "Synthetic protocol fixture."})}}})
            self.stdout.put({"method": "thread/tokenUsage/updated", "params": {"threadId": thread, "turnId": turn,
                "tokenUsage": {"last": {"inputTokens": 20, "outputTokens": 5, "cachedInputTokens": 8, "reasoningOutputTokens": 2},
                               "total": {"inputTokens": 200, "outputTokens": 50, "cachedInputTokens": 80, "reasoningOutputTokens": 20}}}})
            self.stdout.put({"method": "turn/completed", "params": {"threadId": thread, "turn": {"id": turn, "status": "completed"}}})

    def flush(self):
        pass

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = 0
        self.stdout.put(None)

    def kill(self):
        self.terminate()

    def wait(self, timeout=None):
        return self.returncode


def make_host(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts/document_agent.md").write_text("Fictional document test; call get_context only.")
    (tmp_path / "config/workers.json").write_text(json.dumps({"authentication": "chatgpt", "models": {
        "materials": {"model": "gpt-6-luna", "effort": "medium"}}}))

    class Store:
        def __init__(self):
            self.calls = []

        def agent_action(self, worker, capability, action, arguments):
            self.calls.append((worker, capability, action))
            return {"worker_id": worker}

    store = Store()
    host = CodexAppServerRuntime(tmp_path, "codex-fixture", store=store, process_factory=ProtocolProcess,
                                 login_checker=lambda *_: True)
    return host, store


def task(worker, session=None, capability="short-capability"):
    return {"task": {"worker": {"id": worker, "role": "document", "session_id": session}, "task_context": {}},
            "capability": capability}


def test_one_persistent_host_isolates_threads_rotates_capability_and_resumes(tmp_path):
    host, store = make_host(tmp_path)
    ProtocolProcess.instances.clear()
    assert host.status()["status"] == "idle" and not ProtocolProcess.instances
    events = []
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(host.run, task("one"), events.append, lambda: False)
            second = pool.submit(host.run, task("two"), events.append, lambda: False)
            a, b = first.result(timeout=5), second.result(timeout=5)
        assert a["disposition"] == b["disposition"] == "completed"
        assert a["session_id"] != b["session_id"]
        resumed = host.run(task("one", a["session_id"], "rotated-capability"), events.append, lambda: False)
        assert resumed["session_id"] == a["session_id"]
        assert ("one", "rotated-capability", "get_context") in store.calls
        assert len(ProtocolProcess.instances) == 1
        assert ProtocolProcess.instances[0].methods.count("initialize") == 1
        assert ProtocolProcess.instances[0].methods.count("thread/start") == 2
        usage = [event for event in events if event["type"] == "usage"]
        assert len(usage) == 3 and all(event["counter_kind"] == "cumulative" for event in usage)
        assert all(event["counters"]["reasoning_tokens"] == 20 for event in usage)
        assert "short-capability" not in host.registry_path.read_text()
        assert "rotated-capability" not in host.registry_path.read_text()
    finally:
        host.close()
        host.close()


def test_missing_registry_never_falls_back_or_replaces_a_saved_thread(tmp_path):
    host, _ = make_host(tmp_path)
    try:
        result = host.run(task("one", "app_server:lost-thread"), lambda _: None, lambda: False)
        assert result["disposition"] == "session_missing"
        assert "thread/start" not in host._process.methods
        browser = task("browser")
        browser["task"]["worker"]["role"] = "submission"
        assert host.run(browser, lambda _: None, lambda: False)["disposition"] == "failed"
    finally:
        host.close()


def test_document_speeds_are_per_worker_and_loaded_sessions_can_switch_back(tmp_path):
    host, _ = make_host(tmp_path)
    def with_speed(worker, tier, session=None):
        launch = task(worker, session)
        launch["task"]["worker"]["context"] = {"execution": {"provider": "openai", "model": "gpt-6.1-terra",
            "effort": "high", "service_tier": tier, "browser_backend": "none", "transport": "app_server"}}
        return launch
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            fast = pool.submit(host.run, with_speed("fast", "fast"), lambda _: None, lambda: False)
            standard = pool.submit(host.run, with_speed("standard", "default"), lambda _: None, lambda: False)
            a, b = fast.result(timeout=5), standard.result(timeout=5)
        assert a["disposition"] == b["disposition"] == "completed"
        before = len(host._process.requests)
        result = host.run(with_speed("fast", "default", a["session_id"]), lambda _: None, lambda: False)
        assert result["session_id"] == a["session_id"]
        starts = [p for m, p in host._process.requests if m == "thread/start"]
        assert {p["config"]["service_tier"] for p in starts} == {"default", "fast"}
        assert all(p["config"]["features"]["fast_mode"] == (p["config"]["service_tier"] == "fast") for p in starts)
        turns = [p for m, p in host._process.requests if m == "turn/start"]
        assert {p["serviceTier"] for p in turns} == {"default", "fast"}
        last = host._process.requests[before:]
        assert not any(m.startswith("thread/") for m, _ in last)
        assert next(p for m, p in last if m == "turn/start")["serviceTier"] == "default"
    finally:
        host.close()

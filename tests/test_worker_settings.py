from copy import deepcopy
import json
from pathlib import Path
import subprocess

import pytest
from fastapi.testclient import TestClient

from app.store import Store
from app.worker_settings import SettingsConflict
from app.server import create_app
from app.provider_credentials import ProviderCredentials
from app import provider_credentials
from test_efficiency_controls import seed, start
from test_ui_contract import IsolatedAgentManager
from test_agent_runtime import _runtime_root, _task, _FakeProcess
from app import agent_runtime


def choose(store, role, *, provider="deepseek", model="deepseek-flash", effort="high", backend=None):
    settings = store.get_worker_settings()
    roles = deepcopy(settings["roles"])
    roles[role] = {"provider": provider, "model": model, "effort": effort,
                   "browser_backend": backend or ("none" if role == "materials" else "playwright" if provider == "deepseek" else "cua")}
    return store.save_worker_settings(settings["revision"], roles)


def queue_again(store, worker):
    with store.connect(write=True) as db:
        db.execute("UPDATE agent_workers SET state='queued',turn_active=0 WHERE id=?", (worker["id"],))
    assert store.reserve_agent_worker(worker["id"])
    store.agent_launch_context(worker["id"])
    return store.get_agent_worker(worker["id"])


def test_active_turn_keeps_snapshot_next_invocation_switches_and_can_switch_back(tmp_path):
    store, _, discovery, launch, _ = seed(tmp_path, count=1)
    before = store.get_agent_worker(discovery["id"])
    assert before["context"]["execution"]["provider"] == "openai"
    choose(store, "discovery")
    assert store.get_agent_worker(discovery["id"])["context"]["execution"] == before["context"]["execution"]
    assert store.get_agent_worker(discovery["id"])["state"] == "running"
    with store.connect(write=True) as db:
        context = json.loads(db.execute("SELECT context FROM agent_workers WHERE id=?", (discovery["id"],)).fetchone()[0])
        context.update(current_page=3, remaining_pages=[4, 5], tab_id="old-cua-tab")
        db.execute("UPDATE agent_workers SET context=? WHERE id=?", (json.dumps(context), discovery["id"]))
    after = queue_again(store, discovery)
    assert after["context"]["execution"]["provider"] == "deepseek"
    assert after["session_id"] is None
    assert after["context"]["current_page"] == 3
    assert after["context"]["remaining_pages"] == [4, 5]
    assert "tab_id" not in after["context"]
    assert after["context"]["execution_history"][-1]["session_id"] == before["session_id"]
    store.agent_runtime_event(discovery["id"], {"type": "thread.started", "thread_id": "deepseek-test-session"})
    choose(store, "discovery", provider="openai", model="gpt-6-luna", effort="medium")
    returned = queue_again(store, discovery)
    assert returned["session_id"] is None
    assert returned["context"]["execution"]["browser_backend"] == "cua"
    assert returned["context"]["execution_history"][-1]["session_id"] == "deepseek-test-session"
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM agent_execution_runs WHERE worker_id=?", (discovery["id"],)).fetchone()[0] == 3


def test_effort_change_keeps_saved_session_until_next_invocation(tmp_path):
    store, _, discovery, _, _ = seed(tmp_path, count=1)
    original = store.get_agent_worker(discovery["id"])
    choose(store, "discovery", provider="openai", model="gpt-6-luna", effort="low")
    assert store.get_agent_worker(discovery["id"])["context"]["execution"]["effort"] == "medium"
    after = queue_again(store, discovery)
    assert after["session_id"] == original["session_id"]
    assert after["context"]["execution"]["effort"] == "low"


@pytest.mark.parametrize("has_session", [True, False])
def test_possibly_sent_application_defers_provider_switch(tmp_path, has_session):
    store, _, _, _, _ = seed(tmp_path, count=1)
    evaluator = next(w for w in store.list_agent_workers() if w["role"] == "evaluator")
    launch = start(store, evaluator)
    store.agent_action(evaluator["id"], launch["capability"], "finish_evaluation", {"decision": "approve", "reason": "Synthetic supported match", "findings": {}})
    submission = next(w for w in store.list_agent_workers() if w["role"] == "submission")
    start(store, submission)
    with store.connect(write=True) as db:
        row = db.execute("SELECT context FROM agent_workers WHERE id=?", (submission["id"],)).fetchone()
        context = json.loads(row[0]); context["final_action_may_have_been_sent"] = True
        db.execute("UPDATE agent_workers SET context=? WHERE id=?", (json.dumps(context), submission["id"]))
        if not has_session:
            db.execute("UPDATE agent_workers SET session_id=NULL WHERE id=?", (submission["id"],))
    choose(store, "submission")
    after = queue_again(store, submission)
    assert after["context"]["execution"]["provider"] == "openai"
    assert (after["session_id"] is not None) == has_session
    assert after["context"]["final_action_may_have_been_sent"] is True
    assert after["context"]["execution_change_deferred"]["requested"]["provider"] == "deepseek"


def test_validation_and_optimistic_revision_do_not_resume_paused_or_stopped_runs(tmp_path):
    store, pipeline, discovery, _, _ = seed(tmp_path, count=1)
    store.set_agent_quota(True, reason="Synthetic quota hold")
    store.request_pipeline_stop(pipeline["id"])
    initial = store.get_worker_settings()
    invalid = deepcopy(initial["roles"])
    invalid["evaluation"] = {"provider": "deepseek", "model": "deepseek-flash", "effort": "medium", "browser_backend": "playwright"}
    with pytest.raises(ValueError, match="supported model"):
        store.save_worker_settings(0, invalid)
    choose(store, "evaluation")
    with pytest.raises(SettingsConflict):
        store.save_worker_settings(0, initial["roles"])
    assert store.get_agent_quota()["paused"] is True
    assert store.get_pipeline(pipeline["id"])["stop_requested"]
    assert not store.reserve_agent_worker(discovery["id"])


def test_settings_api_auth_revision_and_credentials_never_echo_secret(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_credentials, "_dpapi", lambda value, decrypt=False: value[::-1])
    app = create_app(tmp_path, agent_manager_factory=IsolatedAgentManager)
    with TestClient(app) as client:
        assert client.get("/api/settings/workers").status_code == 403
        client.headers["x-pipeline-token"] = client.get("/api/session").json()["token"]
        saved = client.get("/api/settings/workers").json()
        assert saved["roles"]["submission"]["provider"] == "openai"
        assert client.put("/api/settings/workers", json={"revision": 0, "roles": saved["roles"]}).status_code == 200
        assert client.put("/api/settings/workers", json={"revision": 0, "roles": saved["roles"]}).status_code == 409
        secret = "synthetic-provider-key-only"
        response = client.put("/api/settings/providers/deepseek/key", json={"api_key": secret})
        assert response.status_code == 200 and response.json()["configured"]
        assert secret not in response.text + client.get("/api/settings/workers").text
        assert secret.encode() not in ProviderCredentials(tmp_path).path.read_bytes()
        assert ProviderCredentials(tmp_path).get() == secret
        rejected = client.put("/api/settings/providers/deepseek/key", json={"api_key": secret, "accidental_secret": secret})
        assert rejected.status_code == 422 and secret not in rejected.text
        assert client.delete("/api/settings/providers/deepseek/key").status_code == 200
        assert app.state.agent_pipeline_manager.schedule_calls == 0


def test_deepseek_runtime_uses_scoped_provider_without_touching_subscription_auth(tmp_path, monkeypatch):
    root = _runtime_root(tmp_path)
    (root / "prompts/browser_playwright.md").write_text("Use the approved dedicated Playwright browser.")
    monkeypatch.setattr(agent_runtime.subprocess, "Popen", _FakeProcess)
    monkeypatch.setattr(ProviderCredentials, "get", lambda self: "synthetic-api-key")
    from app import browser_backends
    monkeypatch.setattr(browser_backends, "playwright_args", lambda *args: ["-c", "mcp_servers.playwright.enabled=true"])
    runtime = agent_runtime.CodexAgentRuntime(root, "codex-fixture", login_checker=lambda *_: pytest.fail("DeepSeek must not check or change ChatGPT login"))
    task = _task(); task["worker"]["context"] = {"execution": {"provider": "deepseek", "model": "deepseek-flash", "effort": "low", "browser_backend": "playwright", "transport": "exec"}}
    assert runtime.run(task, lambda event: None, lambda: False)["disposition"] == "completed"
    process = _FakeProcess.instances[-1]
    args = " ".join(process.args)
    assert 'model_provider="deepseek"' in args
    assert "forced_login_method" not in args
    assert "synthetic-api-key" not in args
    assert process.env["DEEPSEEK_API_KEY"] == "synthetic-api-key"
    assert process.env["AGENT_IMAGE_CONTENT_ONLY"] == "1"


def test_playwright_evidence_and_circuit_are_scoped_to_worker(tmp_path):
    store = Store(tmp_path)
    pipeline = store.create_agent_pipeline("Synthetic PW", "https://fictional.invalid/careers")
    choose(store, "discovery")
    worker = store.list_agent_workers(pipeline["id"])[0]; launch = start(store, worker)
    session = store.get_agent_worker(worker["id"])["session_id"]
    event = {"type": "item.completed", "item": {"id": "pw-event", "type": "mcp_tool_call", "server": "playwright", "tool": "browser_navigate", "status": "failed", "error": {"message": "Synthetic browser navigation failure"}}}
    evidence = agent_runtime.CodexAgentRuntime._playwright_tool_result(event, session)
    store.agent_runtime_event(worker["id"], evidence)
    gate = store.agent_action(worker["id"], launch["capability"], "browser_control", {"operation": "open_circuit", "reason": "Synthetic worker browser failure"})
    assert gate["scope"] == "browser:playwright:" + worker["id"]
    with store.connect() as db:
        assert not db.execute("SELECT 1 FROM agent_browser_controls WHERE scope_key='connector:cua_repl' AND state='open'").fetchone()
    assert agent_runtime.CodexAgentRuntime._tool_access_violation({"type": "item.started", "item": {"type": "mcp_tool_call", "server": "playwright", "tool": "browser_snapshot"}}, "discovery", "playwright") is None
    assert agent_runtime.CodexAgentRuntime._tool_access_violation({"type": "item.started", "item": {"type": "mcp_tool_call", "server": "playwright", "tool": "browser_evaluate"}}, "discovery", "playwright")


def test_final_output_envelope_handles_one_json_fence_without_guessing_success():
    value = {"disposition": "completed", "checkpoint": "synthetic", "summary": "Actually recorded"}
    event = lambda text: {"type": "item.completed", "item": {"type": "agent_message", "text": text}}
    fenced = 'Observed outcome.\n```json\n' + json.dumps(value) + '\n```'
    assert agent_runtime._final_response(event(fenced)) == value
    assert agent_runtime._final_response(event(fenced + '\nOther commentary.')) is None
    assert agent_runtime._final_response(event(fenced + '\n' + fenced)) is None
    assert agent_runtime._final_response(event('Done, application submitted.')) is None
    assert agent_runtime._final_response(event(json.dumps({**value, "unknown": True}))) is None


def test_settings_controls_in_real_browser(tmp_path):
    root = Path(__file__).resolve().parents[1]
    from app.browser_backends import playwright_status
    ready = playwright_status(root)
    if not ready["ready"]:
        pytest.skip("Pinned Playwright runtime and installed browser are required for UI acceptance.")
    settings = Store(tmp_path).get_worker_settings(); settings["playwright"] = ready
    fixture = tmp_path / "settings.json"; fixture.write_text(json.dumps(settings), encoding="utf-8")
    result = subprocess.run([ready["node"], str(root / "tests/worker_settings_ui.cjs"), str(fixture), str(tmp_path / "settings.png")],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["passed"] is True

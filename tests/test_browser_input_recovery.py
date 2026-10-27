"""Runtime gates tested with synthetic transport evidence, never real browser work."""
import json

import pytest

from app.store import Conflict
from test_efficiency_controls import start, cua
from test_pipeline_recovery import _saved_submissions


def test_mouse_timeout_is_local_and_keyboard_budget_does_not_open_global_gate(tmp_path):
    store, pipeline, parents = _saved_submissions(tmp_path, 1)
    worker = parents[0]
    launch = start(store, worker)
    failure = "Error: Timed out after 2998ms waiting for CDP command Input.dispatchMouseEvent."
    cua(store, worker, success=False, text=failure)
    result = store.agent_action(worker["id"], launch["capability"], "browser_control", {"operation": "open_circuit"})
    assert result["state"] == "keyboard_fallback" and result["global_circuit_opened"] is False
    assert store.get_agent_worker(worker["id"])["state"] == "running"
    assert store.agent_inbox(pipeline["id"]) == []
    with pytest.raises(Conflict, match="host-managed"):
        store.agent_action(worker["id"], launch["capability"], "checkpoint", {
            "checkpoint": "input", "context": {"input_recovery": {"attempts": 0}}})
    cua(store, worker, success=False, text=failure)
    again = store.agent_action(worker["id"], launch["capability"], "browser_control", {"operation": "open_circuit"})
    assert again["state"] == "job_recovery_exhausted"
    with store.connect() as db:
        assert not db.execute("SELECT 1 FROM agent_browser_controls WHERE control_type='connector_circuit' AND state='open'").fetchone()


def test_mouse_timeout_after_possible_send_cannot_authorize_keyboard_replay(tmp_path):
    store, _, parents = _saved_submissions(tmp_path, 1)
    worker = parents[0]
    launch = start(store, worker)
    with store.connect(write=True) as db:
        db.execute("UPDATE agent_workers SET context=? WHERE id=?", (json.dumps({"final_action_may_have_been_sent": True}), worker["id"]))
    cua(store, worker, success=False, text="Timed out after 5000ms waiting for CDP command Input.dispatchMouseEvent.")
    result = store.agent_action(worker["id"], launch["capability"], "browser_control", {"operation": "open_circuit"})
    assert result["state"] == "reconciliation_only"
    assert "input_recovery" not in store.get_agent_worker(worker["id"])["context"]


def test_real_permission_denial_never_becomes_keyboard_fallback(tmp_path):
    store, _, parents = _saved_submissions(tmp_path, 1)
    worker = parents[0]
    launch = start(store, worker)
    cua(store, worker, success=False, text="Permission denied. Timed out after 5000ms waiting for CDP command Input.dispatchMouseEvent.")
    result = store.agent_action(worker["id"], launch["capability"], "browser_control", {"operation": "open_circuit"})
    assert result["state"] == "waiting_approval"


@pytest.mark.parametrize("command,denied", [("Page.getFrameTree", False), ("Runtime.evaluate", False), ("Runtime.evaluate", True)])
def test_postsend_read_timeout_is_bounded_reconciliation_without_global_gate(tmp_path, command, denied):
    store, pipeline, parents = _saved_submissions(tmp_path, 2)
    worker, other = parents
    launch = start(store, worker)
    with store.connect(write=True) as db:
        db.execute("UPDATE agent_workers SET context=? WHERE id=?", (json.dumps({"final_action_may_have_been_sent": True}), worker["id"]))
    cua(store, worker, success=False, text=("Access denied. " if denied else "") + f"Timed out after 3000ms waiting for CDP command {command}.")
    result = store.agent_action(worker["id"], launch["capability"], "browser_control", {"operation": "open_circuit"})
    if denied:
        assert result["state"] == "waiting_approval"
        assert not store.get_agent_worker(worker["id"])["context"].get("reconciliation_recovery")
        return
    assert result["state"] == "reconciliation_only" and result["read_only_recheck_allowed"] is True
    assert result["global_circuit_opened"] is False and store.agent_inbox(pipeline["id"]) == []
    context = store.get_agent_worker(worker["id"])["context"]
    assert context["final_action_may_have_been_sent"] is True
    assert context["reconciliation_recovery"]["authorization_type"] == "existing_application_scope_read_only"
    assert "approval_item_id" not in context["reconciliation_recovery"]
    assert context["reconciliation_recovery"]["fresh_tab_budget"] == 0
    for action, payload in (("begin_final_action", {}), ("request_documents", {}), ("browser_control", {"operation": "open_circuit"})):
        with pytest.raises(Conflict, match="read-only reconciliation"):
            store.agent_action(worker["id"], launch["capability"], action, payload)
    store.agent_action(worker["id"], launch["capability"], "finish_submission", {"status": "uncertain", "detail": "No receipt; no retry."})
    store.finish_agent_turn(worker["id"], {"disposition": "completed"})
    assert store.reserve_agent_worker(other["id"])


def test_company_form_writer_slot_is_released_while_documents_run(tmp_path):
    store, _, parents = _saved_submissions(tmp_path, 2)
    (tmp_path / "config").mkdir(exist_ok=True)
    (tmp_path / "config/workers.json").write_text(json.dumps({"efficiency": {"max_concurrent_form_writers": 1}}))
    assert store.reserve_agent_worker(parents[0]["id"])
    assert not store.reserve_agent_worker(parents[1]["id"])
    with store.connect(write=True) as db:
        db.execute("UPDATE agent_workers SET state='waiting_child',turn_active=0 WHERE id=?", (parents[0]["id"],))
    assert store.reserve_agent_worker(parents[1]["id"])


def test_failed_global_recovery_without_inbox_is_visible_as_fault(tmp_path):
    store, pipeline, parents = _saved_submissions(tmp_path, 2)
    worker = parents[0]
    with store.connect(write=True) as db:
        db.execute("UPDATE agent_workers SET state='completed',turn_active=0 WHERE role='discovery'")
    launch = start(store, worker)
    cua(store, worker, success=False, text="Synthetic disconnected browser")
    gate = store.agent_action(worker["id"], launch["capability"], "browser_control", {"operation": "open_circuit"})
    store.finish_agent_turn(worker["id"], {"disposition": "waiting"})
    store.decide_agent_inbox(gate["inbox_item_id"], "approve")
    start(store, store.get_agent_worker(worker["id"]))
    store.finish_agent_turn(worker["id"], {"disposition": "failed", "error": "Synthetic approved recheck failure"})
    result = store.get_pipeline(pipeline["id"])
    assert result["status"] == "blocked" and result["stage"] == "browser_fault"
    assert all(i["state"] != "pending" for i in store.agent_inbox(pipeline["id"]))


def test_profile_cannot_clear_amazon_form_refresh_and_auth_metadata_keeps_no_secrets(tmp_path):
    store, pipeline, parents = _saved_submissions(tmp_path, 1)
    worker = parents[0]
    with store.connect(write=True) as db:
        db.execute("UPDATE jobs SET url='https://www.amazon.jobs/jobs/0' WHERE id=?", (worker["job_id"],))
        db.execute("UPDATE agent_workers SET state='quota_paused' WHERE id=?", (worker["id"],))
        db.execute("INSERT INTO meta(key,value) VALUES(?,?)", ("agent_signin_automation:" + pipeline["id"], json.dumps({"enabled": True})))
    store.set_agent_quota(True, reason="Synthetic quota")
    store.resume_agent_quota()
    worker = store.get_agent_worker(worker["id"])
    launch = start(store, worker)
    cua(store, worker)
    store.agent_action(worker["id"], launch["capability"], "browser_control", {"operation": "acquire_auth"})

    def observed(url):
        return store.agent_runtime_event(worker["id"], {"type": "tool_result", "tool_name": "cua_repl.js", "success": True,
            "session_id": worker["session_id"], "transport_event": {"type": "item.completed", "item": {
                "type": "mcp_tool_call", "server": "cua_repl", "tool": "js", "status": "completed",
                "result": {"_meta": {"browser_use": {"url": url}},
                           "content": [{"type": "text", "text": "SENSITIVE_SESSION_TOKEN"}]}}}})

    observed("https://www.amazon.jobs/user/details?token=SENSITIVE_SESSION_TOKEN")
    with pytest.raises(Conflict, match="profile or dashboard"):
        store.agent_action(worker["id"], launch["capability"], "browser_control", {"operation": "complete_auth"})
    observed("https://www.amazon.jobs/en-US/applicant/jobs/999/apply?token=SENSITIVE_SESSION_TOKEN")
    with pytest.raises(Conflict, match="profile or dashboard"):
        store.agent_action(worker["id"], launch["capability"], "browser_control", {"operation": "complete_auth"})
    event = observed("https://www.amazon.jobs/en-US/applicant/jobs/0/apply?token=SENSITIVE_SESSION_TOKEN")
    with store.connect() as db:
        stored = db.execute("SELECT payload FROM agent_runtime_events WHERE id=?", (event,)).fetchone()[0]
    assert "SENSITIVE_SESSION_TOKEN" not in stored
    assert json.loads(stored)["observed_application_url"] == "https://www.amazon.jobs/en-US/applicant/jobs/0/apply"
    store.agent_action(worker["id"], launch["capability"], "browser_control", {"operation": "complete_auth"})
    assert store.get_agent_worker(worker["id"])["context"]["browser_freshness"]["pending"] is False

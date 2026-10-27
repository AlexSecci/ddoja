"""Saved-worker recovery, without models or employer/browser operations."""
import json

import pytest

from app.store import Conflict
from test_efficiency_controls import seed, start, cua


def _saved_submissions(tmp_path, count=2):
    store, pipeline, _, _, _ = seed(tmp_path, count)
    for evaluator in [w for w in store.list_agent_workers(pipeline["id"]) if w["role"] == "evaluator"]:
        launch = start(store, evaluator)
        store.agent_action(evaluator["id"], launch["capability"], "finish_evaluation", {"decision": "approve", "reason": "Fixture"})
        store.finish_agent_turn(evaluator["id"], {"disposition": "completed"})
    parents = [w for w in store.list_agent_workers(pipeline["id"]) if w["role"] == "submission"]
    with store.connect(write=True) as db:
        for worker in parents:
            context = {"tab_id": "saved-tab", "unsaved_answers": {"confirmed_fact": "preserve me"},
                       "tab_recovery": {"attempt_id": "old-spent-attempt"}}
            db.execute("UPDATE agent_workers SET session_id=?,context=?,checkpoint='saved_form' WHERE id=?",
                       ("saved-" + worker["id"], json.dumps(context), worker["id"]))
    return store, pipeline, [store.get_agent_worker(w["id"]) for w in parents]


def test_quota_resume_refreshes_old_pages_including_pending_approvals_once(tmp_path):
    store, pipeline, parents = _saved_submissions(tmp_path)
    with store.connect(write=True) as db:
        db.execute("UPDATE agent_workers SET state='quota_paused' WHERE id=?", (parents[0]["id"],))
        raw = db.execute("SELECT * FROM agent_workers WHERE id=?", (parents[1]["id"],)).fetchone()
        inbox, _ = store._agent_insert_inbox(db, raw, "approval", action="binding_agreement",
            context={"jd_version": raw["jd_version"], "profile_version": raw["profile_version"]})
    store.set_agent_quota(True, reason="Fixture pause lasting hours")
    store.resume_agent_quota()
    resumed = store.get_agent_worker(parents[0]["id"])
    waiting = store.get_agent_worker(parents[1]["id"])
    assert resumed["session_id"] == parents[0]["session_id"] and resumed["resume_mode"] == "same_session"
    assert resumed["checkpoint"] == "saved_form" and resumed["context"]["unsaved_answers"] == {"confirmed_fact": "preserve me"}
    assert resumed["context"]["tab_recovery"]["attempt_id"] != "old-spent-attempt"
    assert resumed["context"]["tab_recovery"]["fresh_tab_budget"] == 1
    assert resumed["context"]["browser_freshness"]["pending"] is True
    assert waiting["state"] == "waiting_approval" and waiting["context"]["browser_freshness"]["pending"] is True
    assert next(i for i in store.agent_inbox(pipeline["id"]) if i["id"] == inbox["id"])["state"] == "pending"
    original = resumed["context"]["tab_recovery"]
    store.resume_agent_quota()
    assert store.get_agent_worker(resumed["id"])["context"]["tab_recovery"] == original


def test_stale_application_cannot_submit_until_new_browser_auth_observation(tmp_path):
    store, pipeline, parents = _saved_submissions(tmp_path, 1)
    parent = parents[0]
    with store.connect(write=True) as db:
        db.execute("UPDATE agent_workers SET state='quota_paused' WHERE id=?", (parent["id"],))
    store.set_agent_quota(True, reason="Fixture expired website")
    store.resume_agent_quota()
    parent = store.get_agent_worker(parent["id"])
    launch = start(store, parent)
    controls = store.agent_action(parent["id"], launch["capability"], "get_context", {})
    assert controls["runtime_controls"]["browser_freshness"]["pending"] is True
    with pytest.raises(Conflict, match="host-managed"):
        store.agent_action(parent["id"], launch["capability"], "checkpoint", {
            "checkpoint": "pretend_refreshed", "context": {"browser_freshness": {"pending": False}}})
    store.agent_action(parent["id"], launch["capability"], "checkpoint", {
        "checkpoint": "refresh_started", "context": {"browser_refresh_progress": {"reload_done": True}}})
    with pytest.raises(Conflict, match="page may have expired"):
        store.agent_action(parent["id"], launch["capability"], "begin_final_action", {})
    cua(store, parent, text="Current owned tab before refresh validation")
    store.agent_action(parent["id"], launch["capability"], "browser_control", {"operation": "acquire_auth"})
    with pytest.raises(Conflict, match="new successful"):
        store.agent_action(parent["id"], launch["capability"], "browser_control", {"operation": "complete_auth"})
    event = cua(store, parent, text="Freshly reopened exact application and intended account, saved facts verified")
    store.agent_action(parent["id"], launch["capability"], "browser_control", {"operation": "complete_auth"})
    freshness = store.get_agent_worker(parent["id"])["context"]["browser_freshness"]
    assert freshness["pending"] is False and freshness["evidence_id"] == event
    # This now reaches the normal first-application review guard, not the stale-page guard.
    with pytest.raises(Conflict, match="first application"):
        store.agent_action(parent["id"], launch["capability"], "begin_final_action", {})


def test_quota_refresh_does_not_touch_stopped_campaign_or_possibly_sent_application(tmp_path):
    store, pipeline, parents = _saved_submissions(tmp_path)
    with store.connect(write=True) as db:
        db.execute("UPDATE agent_workers SET state='quota_paused',context=? WHERE id=?",
                   (json.dumps({"final_action_may_have_been_sent": True}), parents[0]["id"]))
    stopped = store.create_agent_pipeline("Stopped fixture", "https://stopped.invalid")
    stopped_worker = store.list_agent_workers(stopped["id"])[0]
    with store.connect(write=True) as db:
        db.execute("UPDATE pipelines SET status='stopped',stop_requested=1 WHERE id=?", (stopped["id"],))
        db.execute("UPDATE agent_workers SET state='quota_paused',session_id='saved-stopped' WHERE id=?", (stopped_worker["id"],))
    store.set_agent_quota(True, reason="Fixture")
    store.resume_agent_quota()
    uncertain = store.get_agent_worker(parents[0]["id"])
    assert uncertain["state"] == "uncertain" and "tab_recovery" not in uncertain["context"]
    assert "browser_freshness" not in uncertain["context"]
    untouched = store.get_agent_worker(stopped_worker["id"])
    assert untouched["state"] == "quota_paused" and "tab_recovery" not in untouched["context"]


def test_company_resume_recovers_quota_workers_after_quota_was_cleared_while_stopped(tmp_path):
    store, pipeline, discovery, _, _ = seed(tmp_path, 1)
    evaluator = next(w for w in store.list_agent_workers(pipeline["id"]) if w["role"] == "evaluator")
    with store.connect(write=True) as db:
        db.execute("UPDATE agent_workers SET state='quota_paused',turn_active=0,session_id=?,checkpoint='saved_page_3' WHERE id=?",
                   ("saved-discovery-session", discovery["id"]))
        db.execute("UPDATE agent_workers SET state='interrupted',session_id='saved-evaluator-session' WHERE id=?", (evaluator["id"],))
        db.execute("UPDATE pipelines SET status='stopped',stop_requested=1 WHERE id=?", (pipeline["id"],))
    store.set_agent_quota(True, reason="Synthetic quota")
    store.resume_agent_quota()
    assert store.get_agent_worker(discovery["id"])["state"] == "quota_paused"
    store.resume_agent_pipeline(pipeline["id"])
    for worker in (discovery, evaluator):
        resumed = store.get_agent_worker(worker["id"])
        assert resumed["state"] == "queued" and resumed["resume_mode"] == "same_session"
        assert resumed["context"]["tab_recovery"]["fresh_tab_budget"] == 1
    assert store.get_agent_worker(discovery["id"])["checkpoint"] == "saved_page_3"
    first_attempt = store.get_agent_worker(discovery["id"])["context"]["tab_recovery"]["attempt_id"]
    with store.connect(write=True) as db:
        db.execute("UPDATE agent_workers SET state='interrupted' WHERE id=?", (discovery["id"],))
    store.resume_agent_pipeline(pipeline["id"])
    assert store.get_agent_worker(discovery["id"])["context"]["tab_recovery"]["attempt_id"] != first_attempt


def test_quota_resume_is_idempotent_and_repairs_stale_paused_workers_without_touching_stopped_companies(tmp_path):
    store, pipeline, discovery, _, _ = seed(tmp_path, 0)
    stopped = store.create_agent_pipeline("Stopped company", "https://stopped.invalid/jobs")
    stopped_worker = store.list_agent_workers(stopped["id"])[0]
    with store.connect(write=True) as db:
        db.execute("UPDATE agent_workers SET state='quota_paused',turn_active=0 WHERE id IN (?,?)", (discovery["id"], stopped_worker["id"]))
        db.execute("UPDATE pipelines SET stop_requested=1,status='stopped' WHERE id=?", (stopped["id"],))
    assert not store.get_agent_quota()["paused"]
    store.resume_agent_quota()
    assert store.get_agent_worker(discovery["id"])["state"] == "queued"
    assert store.get_agent_worker(stopped_worker["id"])["state"] == "quota_paused"
    store.resume_agent_quota()
    assert store.get_agent_worker(discovery["id"])["state"] == "queued"


def test_quota_recovery_does_not_replay_a_possibly_sent_application(tmp_path):
    store, pipeline, _, _, _ = seed(tmp_path, 1)
    evaluator = next(w for w in store.list_agent_workers(pipeline["id"]) if w["role"] == "evaluator")
    launch = start(store, evaluator)
    result = store.agent_action(evaluator["id"], launch["capability"], "finish_evaluation", {"decision": "approve", "reason": "Fictional match"})
    with store.connect(write=True) as db:
        db.execute("UPDATE agent_workers SET state='quota_paused',context=? WHERE id=?",
                   (json.dumps({"final_action_may_have_been_sent": True}), result["submission_worker_id"]))
    store.resume_agent_pipeline(pipeline["id"])
    current = store.get_agent_worker(result["submission_worker_id"])
    assert current["state"] == "uncertain"
    assert "tab_recovery" not in current["context"]


def test_failed_login_owner_wakes_one_waiter_but_does_not_claim_authentication_succeeded(tmp_path):
    store, pipeline, _, _, _ = seed(tmp_path, 2)
    owners = [w for w in store.list_agent_workers(pipeline["id"]) if w["role"] == "evaluator"]
    a, b = start(store, owners[0]), start(store, owners[1])
    cua(store, owners[0])
    store.agent_action(owners[0]["id"], a["capability"], "browser_control", {"operation": "acquire_auth"})
    cua(store, owners[1])
    assert store.agent_action(owners[1]["id"], b["capability"], "browser_control", {"operation": "acquire_auth"})["state"] == "waiting_browser"
    store.finish_agent_turn(owners[1]["id"], {"disposition": "waiting"})
    store.finish_agent_turn(owners[0]["id"], {"disposition": "failed", "error": "Synthetic missing tab"})
    assert store.get_agent_worker(owners[1]["id"])["state"] == "queued"
    resumed = start(store, store.get_agent_worker(owners[1]["id"]))
    with store.connect() as db:
        gate = db.execute("SELECT * FROM agent_browser_controls WHERE control_type='company_auth'").fetchone()
        assert gate["state"] == "open" and gate["worker_id"] == owners[1]["id"]
    with pytest.raises(Conflict, match="new successful"):
        store.agent_action(owners[1]["id"], resumed["capability"], "browser_control", {"operation": "complete_auth"})
    cua(store, owners[1], text="Fresh intended account observation")
    assert store.agent_action(owners[1]["id"], resumed["capability"], "browser_control", {"operation": "complete_auth"})["state"] == "closed"


def test_queued_submission_can_take_over_a_failed_owner_but_never_an_active_one(tmp_path):
    store, pipeline, _, _, _ = seed(tmp_path, 2)
    submissions = []
    for evaluator in [w for w in store.list_agent_workers(pipeline["id"]) if w["role"] == "evaluator"]:
        launch = start(store, evaluator)
        result = store.agent_action(evaluator["id"], launch["capability"], "finish_evaluation", {"decision": "approve", "reason": "Fictional match"})
        store.finish_agent_turn(evaluator["id"], {"disposition": "completed"})
        submissions.append(store.get_agent_worker(result["submission_worker_id"]))
    launch = start(store, submissions[0])
    cua(store, submissions[0])
    store.agent_action(submissions[0]["id"], launch["capability"], "browser_control", {"operation": "acquire_auth"})
    with store.connect(write=True) as db:
        db.execute("UPDATE agent_workers SET state='failed' WHERE id=?", (submissions[0]["id"],))
    assert not store.reserve_agent_worker(submissions[1]["id"])
    store.finish_agent_turn(submissions[0]["id"], {"disposition": "failed"})
    assert store.reserve_agent_worker(submissions[1]["id"])
    with store.connect() as db:
        gate = db.execute("SELECT state,worker_id FROM agent_browser_controls WHERE control_type='company_auth'").fetchone()
        assert gate["state"] == "open" and gate["worker_id"] == submissions[1]["id"]


def test_resume_does_not_bypass_an_unanswered_action_approval(tmp_path):
    store, pipeline, _, _, _ = seed(tmp_path, 1)
    worker = next(w for w in store.list_agent_workers(pipeline["id"]) if w["role"] == "evaluator")
    with store.connect(write=True) as db:
        row = db.execute("SELECT * FROM agent_workers WHERE id=?", (worker["id"],)).fetchone()
        item, _ = store._agent_insert_inbox(db, row, "approval", action="exact_checkpoint", summary="Unanswered approval")
        db.execute("UPDATE agent_workers SET state='quota_paused' WHERE id=?", (worker["id"],))
    store.resume_agent_pipeline(pipeline["id"])
    assert store.get_agent_worker(worker["id"])["state"] == "waiting_approval"
    assert next(i for i in store.agent_inbox(pipeline["id"]) if i["id"] == item["id"])["state"] == "pending"


def test_authorized_email_signin_replaces_only_the_exact_handoff_and_redacts_auth_payloads(tmp_path):
    store, pipeline, discovery, discovery_launch, _ = seed(tmp_path, 1)
    evaluator = next(w for w in store.list_agent_workers(pipeline["id"]) if w["role"] == "evaluator")
    launch = start(store, evaluator)
    result = store.agent_action(evaluator["id"], launch["capability"], "finish_evaluation", {"decision": "approve", "reason": "Fictional match"})
    store.finish_agent_turn(evaluator["id"], {"disposition": "completed"})
    submission = store.get_agent_worker(result["submission_worker_id"])
    launch = start(store, submission)
    cua(store, submission, text="Fictional sign-in page")
    store.agent_action(submission["id"], launch["capability"], "browser_control", {"operation": "acquire_auth"})
    question = store.agent_action(submission["id"], launch["capability"], "ask_question", {"question": "Complete sign-in in the owned tab."})
    store.finish_agent_turn(submission["id"], {"disposition": "waiting"})
    store.authorize_agent_signin(pipeline["id"], "candidate@example.test", "User explicitly authorized routine autofill and employer email OTP.", question["inbox_item_id"])
    item = next(i for i in store.agent_inbox(pipeline["id"]) if i["id"] == question["inbox_item_id"])
    assert item["state"] == "stale" and item["response"] == ""
    store.resume_agent_pipeline(pipeline["id"])
    launch = start(store, store.get_agent_worker(submission["id"]))
    controls = launch["task"]["task_context"]["runtime_controls"]
    assert controls["signin_automation"]["email_otp"] is True
    assert "signin_automation" not in store.agent_action(discovery["id"], discovery_launch["capability"], "get_context", {})["runtime_controls"]
    secret = "SYNTHETIC-PRIVATE-CODE-DO-NOT-PERSIST"
    event_id = cua(store, submission, text=secret)
    with store.connect() as db:
        payload = db.execute("SELECT payload FROM agent_runtime_events WHERE id=?", (event_id,)).fetchone()[0]
        assert secret not in payload
        assert json.loads(payload)["authentication_redacted"] is True
        gate = db.execute("SELECT state FROM agent_browser_controls WHERE control_type='company_auth'").fetchone()
        assert gate["state"] == "open"
    assert store.agent_action(submission["id"], launch["capability"], "browser_control", {"operation": "complete_auth"})["state"] == "closed"


def test_browser_skill_reading_has_a_scoped_bridge_path_without_enabling_shell(tmp_path, monkeypatch):
    from tools.agent_mcp import AgentMCPBridge
    from app.agent_runtime import CodexAgentRuntime
    from pathlib import Path
    fixture_home = tmp_path / "synthetic-home"
    skill = fixture_home / ".codex/plugins/cache/openai-bundled/computer-use/1.0.0/skills/computer-use/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("# Computer Use\nSynthetic complete skill content.\n", encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: fixture_home)
    store, pipeline, discovery, launch, _ = seed(tmp_path, 0)
    bridge = AgentMCPBridge(discovery["id"], launch["capability"], "discovery", store.agent_action)
    # Simulate the installed layout without depending on this user's machine.
    guidance = bridge.call("get_browser_guidance", {"document": "skill"})
    assert guidance["source_path"].endswith("SKILL.md")
    assert "Computer Use" in guidance["content"]
    with pytest.raises(ValueError):
        bridge.call("get_browser_guidance", {"document": "../../other-file"})
    assert CodexAgentRuntime._tool_access_violation({"type": "item.started", "item": {"type": "command_execution"}}, "evaluator") == "command_execution"


def test_resuming_document_child_restores_the_same_parent_dependency(tmp_path):
    store, pipeline, _, _, _ = seed(tmp_path, 1)
    evaluator = next(w for w in store.list_agent_workers(pipeline["id"]) if w["role"] == "evaluator")
    launch = start(store, evaluator)
    result = store.agent_action(evaluator["id"], launch["capability"], "finish_evaluation", {"decision": "approve", "reason": "Fictional match"})
    store.finish_agent_turn(evaluator["id"], {"disposition": "completed"})
    parent = store.get_agent_worker(result["submission_worker_id"])
    launch = start(store, parent)
    request = store.agent_action(parent["id"], launch["capability"], "request_documents", {"artifacts": ["resume"], "reason": "Observed field"})
    child_id = request["child_worker_id"]
    store.finish_agent_turn(parent["id"], {"disposition": "waiting"})
    store.request_agent_pipeline_stop(pipeline["id"])
    store.resume_agent_pipeline(pipeline["id"])
    assert store.get_agent_worker(parent["id"])["state"] == "waiting_child"
    assert store.get_agent_worker(child_id)["state"] == "queued"
    assert not store.reserve_agent_worker(parent["id"])
    assert store.reserve_agent_worker(child_id)

import json

import pytest

from app.store import Conflict, timestamp
from test_batch_approvals import fixture
from test_efficiency_controls import seed, start, cua
from test_pipeline_recovery import _saved_submissions


def recovery_fixture(tmp_path):
    _, _, _, store, pid, items, _ = fixture(tmp_path)
    item = items[0]
    with store.connect(write=True) as db:
        db.execute("UPDATE agent_inbox SET action='browser_circuit_recovery',context=? WHERE id=?",
                   (json.dumps({"scope": "connector:cua_repl", "source_event_id": "fictional-failure"}), item["id"]))
        db.execute("INSERT INTO agent_browser_controls(scope_key,control_type,state,worker_id,event_ids,updated_at) "
                   "VALUES('connector:cua_repl','connector_circuit','open',?,?,?)",
                   (item["worker_id"], json.dumps(["fictional-failure"]), timestamp()))
    return store, pid, item


def test_approval_grants_a_new_attempt_once_and_rejection_does_not(tmp_path):
    store, pid, item = recovery_fixture(tmp_path)
    with store.connect(write=True) as db:
        db.execute("UPDATE agent_workers SET context=? WHERE id=?", (json.dumps({"tab_recovery": {
            "attempt_id": "spent-before-approval", "fresh_tab_budget": 1}, "tab_recovery_attempt_id": "spent-before-approval"}), item["worker_id"]))
    store.decide_agent_inbox(item["id"], "approve")
    recovery = store.get_agent_worker(item["worker_id"])["context"]["tab_recovery"]
    assert recovery["attempt_id"] != "spent-before-approval"
    assert recovery["approval_item_id"] == item["id"] and recovery["fresh_tab_budget"] == 1
    with store.connect(write=True) as db:
        assert store._agent_grant_browser_recovery_attempt(db, item["id"], timestamp()) is False
    other = next(i for i in store.agent_inbox(pid) if i["id"] != item["id"])
    store.decide_agent_inbox(other["id"], "reject")
    assert "tab_recovery" not in store.get_agent_worker(other["worker_id"])["context"]


def test_post_send_recovery_approval_grants_inspection_without_a_tab_or_send_retry(tmp_path):
    store, pid, item = recovery_fixture(tmp_path)
    with store.connect(write=True) as db:
        db.execute("UPDATE agent_workers SET role='submission',context=?,session_id='saved-session' WHERE id=?",
                   (json.dumps({"final_action_may_have_been_sent": True}), item["worker_id"]))
    # This previously rolled back the human decision with HTTP 409 because a
    # confirmation-page inspection was incorrectly treated as a fresh-tab retry.
    decided = store.decide_agent_inbox(item["id"], "approve", "Human approved one read-only check")
    assert decided["state"] == "approved"
    worker = store.get_agent_worker(item["worker_id"])
    assert worker["state"] == "queued" and worker["session_id"] == "saved-session"
    assert worker["context"]["final_action_may_have_been_sent"] is True
    assert "tab_recovery" not in worker["context"]
    recovery = worker["context"]["reconciliation_recovery"]
    assert recovery["read_only"] is True and recovery["fresh_tab_budget"] == 0
    assert recovery["final_action_replay_authorized"] is False
    with store.connect(write=True) as db:
        assert store._agent_grant_browser_recovery_attempt(db, item["id"], timestamp()) is False

    launch = start(store, worker)
    controls = store.agent_action(worker["id"], launch["capability"], "get_context", {})["runtime_controls"]
    assert controls["reconciliation_recovery"]["read_only"] is True
    for action, payload in (("begin_final_action", {}), ("request_documents", {}),
                            ("browser_control", {"operation": "acquire_auth"}),
                            ("finish_submission", {"status": "failed"})):
        with pytest.raises(Conflict, match="read-only reconciliation"):
            store.agent_action(worker["id"], launch["capability"], action, payload)
    with pytest.raises(Conflict, match="host-managed|host-owned"):
        store.agent_action(worker["id"], launch["capability"], "checkpoint", {
            "checkpoint": "inspection", "context": {"reconciliation_recovery": {}}})
    cua(store, worker, success=True)
    done = store.agent_action(worker["id"], launch["capability"], "browser_control", {"operation": "complete_recovery"})
    assert done["state"] == "closed"
    with pytest.raises(Conflict, match="read-only reconciliation"):
        store.agent_action(worker["id"], launch["capability"], "begin_final_action", {})


def test_rejected_post_send_recovery_grants_no_inspection_authority(tmp_path):
    store, _, item = recovery_fixture(tmp_path)
    with store.connect(write=True) as db:
        db.execute("UPDATE agent_workers SET context=? WHERE id=?",
                   (json.dumps({"final_action_may_have_been_sent": True}), item["worker_id"]))
    store.decide_agent_inbox(item["id"], "reject")
    context = store.get_agent_worker(item["worker_id"])["context"]
    assert context["final_action_may_have_been_sent"] is True
    assert "reconciliation_recovery" not in context and "tab_recovery" not in context


@pytest.mark.parametrize("tab_present,denied", [(False, False), (True, False), (False, True)])
def test_uncertain_missing_tab_releases_only_verified_local_connector_lock(tmp_path, tab_present, denied):
    store, pipeline, parents = _saved_submissions(tmp_path, 2)
    worker, other = parents
    launch = start(store, worker)
    with store.connect(write=True) as db:
        db.execute("UPDATE agent_workers SET context=? WHERE id=?", (json.dumps({
            "final_action_may_have_been_sent": True, "tab_id": "41"}), worker["id"]))
    failure = ("Access denied. " if denied else "") + "Timed out after 10000ms waiting for CDP command Page.getFrameTree."
    failure_id = cua(store, worker, success=False, text=failure)
    # Reproduce a checkpoint emitted by the older global-timeout handler.
    # New post-send technical timeouts now stay local and create no approval.
    with store.connect(write=True) as db:
        current = db.execute("SELECT * FROM agent_workers WHERE id=?", (worker["id"],)).fetchone()
        store._agent_set_browser_control(db, "connector:cua_repl", "connector_circuit", "open",
                                        failure, "Legacy timeout handler", worker["id"], [failure_id])
        item, _ = store._agent_insert_inbox(db, current, "approval", action="browser_circuit_recovery",
            summary="Legacy post-send observation recovery", context={"scope": "connector:cua_repl", "source_event_id": failure_id})
        gate = {"inbox_item_id": item["id"]}
    store.finish_agent_turn(worker["id"], {"disposition": "waiting"})
    store.decide_agent_inbox(gate["inbox_item_id"], "approve", "Human approved read-only inspection")
    launch = start(store, store.get_agent_worker(worker["id"]))
    cua(store, worker, text=json.dumps({"browsers": [{"id": "chrome", "family": "chrome", "type": "extension",
        "tabs": [{"id": "41" if tab_present else "42"}]}]}))
    result = store.agent_action(worker["id"], launch["capability"], "finish_submission", {
        "status": "uncertain", "detail": "No verified receipt; preserve possible send.", "observations": {}})
    store.finish_agent_turn(worker["id"], {"disposition": "completed"})
    assert result["status"] == "uncertain"
    assert store.get_agent_worker(worker["id"])["context"]["final_action_may_have_been_sent"] is True
    assert store.reserve_agent_worker(other["id"]) is (not tab_present and not denied)
    with store.connect() as db:
        assert db.execute("SELECT state FROM agent_browser_controls WHERE scope_key='connector:cua_repl'").fetchone()[0] == ("closed" if not tab_present and not denied else "open")


@pytest.mark.parametrize("possibly_sent", [False, True])
def test_repair_uses_existing_approval_and_never_retries_a_possible_send(tmp_path, possibly_sent):
    store, _, item = recovery_fixture(tmp_path)
    store.decide_agent_inbox(item["id"], "approve")
    context = {"tab_recovery": {"attempt_id": "legacy-spent"}}
    if possibly_sent:
        context["final_action_may_have_been_sent"] = True
    observed = {"tab_missing_from_fresh_inventory": True, "final_action_started": False,
                "connector_recovery_approval_id": item["id"], "tab_recovery_attempt_id": "legacy-spent"}
    with store.connect(write=True) as db:
        db.execute("UPDATE agent_workers SET state='failed',checkpoint='recovery_tab_missing',context=?,session_id='saved' WHERE id=?",
                   (json.dumps(context), item["worker_id"]))
        db.execute("INSERT INTO agent_submission_results(id,pipeline_id,worker_id,job_id,state,observations,created_at) VALUES(?,?,?,?,?,?,?)",
                   ("fictional-result", item["pipeline_id"], item["worker_id"], item["job_id"], "failed", json.dumps(observed), timestamp()))
    if possibly_sent:
        with pytest.raises(Conflict):
            store.repair_approved_missing_tab_recovery(item["id"])
    else:
        repaired = store.repair_approved_missing_tab_recovery(item["id"])
        assert repaired["state"] == "queued" and repaired["resume_mode"] == "same_session"
        assert repaired["context"]["tab_recovery"]["approval_item_id"] == item["id"]
        with pytest.raises(Conflict):
            store.repair_approved_missing_tab_recovery(item["id"])


@pytest.mark.parametrize("live_auth_owner", [False, True])
def test_connector_recovery_does_not_deadlock_with_an_idle_auth_mutex(tmp_path, live_auth_owner):
    store, pipeline, _, _, _ = seed(tmp_path, 2)
    for evaluator in [w for w in store.list_agent_workers(pipeline["id"]) if w["role"] == "evaluator"]:
        launch = start(store, evaluator)
        store.agent_action(evaluator["id"], launch["capability"], "finish_evaluation", {"decision": "approve", "reason": "Fixture"})
        store.finish_agent_turn(evaluator["id"], {"disposition": "completed"})
    owner, idle = [w for w in store.list_agent_workers(pipeline["id"]) if w["role"] == "submission"]
    launch = start(store, owner)
    cua(store, owner, success=False)
    gate = store.agent_action(owner["id"], launch["capability"], "browser_control", {"operation": "open_circuit"})
    store.finish_agent_turn(owner["id"], {"disposition": "waiting"})
    with store.connect(write=True) as db:
        store._agent_set_browser_control(db, "company:" + pipeline["company_id"], "company_auth", "open",
                                        "Fictional sign-in needed", "Fixture", idle["id"], [])
        if live_auth_owner:
            db.execute("UPDATE agent_workers SET state='running',turn_active=1 WHERE id=?", (idle["id"],))
    store.decide_agent_inbox(gate["inbox_item_id"], "approve")
    assert store.reserve_agent_worker(owner["id"]) is (not live_auth_owner)
    with store.connect() as db:
        auth = db.execute("SELECT * FROM agent_browser_controls WHERE control_type='company_auth'").fetchone()
        assert auth["state"] == "open"  # Ownership never pretends authentication succeeded.
        assert auth["worker_id"] == (idle["id"] if live_auth_owner else owner["id"])


def test_current_controls_deliver_shared_grouping_to_an_existing_worker(tmp_path):
    store, _, worker, launch, _ = seed(tmp_path, 1)
    before = store.agent_action(worker["id"], launch["capability"], "get_context", {})
    assert "browser_grouping" not in before["runtime_controls"]
    store.set_browser_grouping("User requested two shared stage groups, with evaluators sharing Applications.")
    after = store.agent_action(worker["id"], launch["capability"], "get_context", {})
    assert after["runtime_controls"]["browser_grouping"]["applications"] == "Pipeline · Applications"
    assert after["runtime_controls"]["browser_grouping"]["max_groups"] == 2

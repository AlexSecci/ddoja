"""Real Store/HTTP transactions; fictional jobs, no worker runtime or browser."""
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.store import Conflict, Store, timestamp
from test_ui_contract import _client, _create_pipeline, _seed_inbox, _seed_listing


def fixture(tmp_path):
    app, client, headers, manager = _client(tmp_path)
    store = Store(tmp_path)
    view = _create_pipeline(client, headers)
    pipeline_id = view["pipeline"]["id"]
    job = store.get_job(_seed_listing(store, view["pipeline"]["company_id"])["id"])
    with store.connect(write=True) as db:
        db.execute("INSERT INTO pipeline_jobs(pipeline_id,job_id,discovered_at,selected,agent_status) "
                   "VALUES(?,?,?,1,'selected')", (pipeline_id, job["id"], timestamp()))
    for number in range(2):
        _seed_inbox(store, pipeline_id, job["id"], "approval", action="binding_agreement",
                    summary=f"Accept fictional terms {number}", context={
                        "exact_terms": {"text": f"Fictional terms version {number}"},
                        "jd_version": job["jd_version"], "profile_version": store.profile_version(),
                        "ready_artifacts": [], "job_url": job["url"],
                    })
    items = store.agent_inbox(pipeline_id)
    selected = [{"id": item["id"], "scope_token": item["scope_token"]} for item in items]
    return client, headers, manager, store, pipeline_id, items, selected


def test_exact_batch_is_authenticated_atomic_and_has_per_item_receipts(tmp_path):
    client, headers, manager, store, pid, items, selected = fixture(tmp_path)
    route = f"/api/pipelines/{pid}/inbox/batch-decision"
    assert all(item["batch_eligible"] for item in items)
    assert client.post(route, json={"items": selected, "decision": "approve"}).status_code == 403
    with store.connect(write=True) as db:
        db.execute("UPDATE agent_workers SET session_id='saved-session' WHERE id=?", (items[0]["worker_id"],))
        # An inbox request can be approved before its emitting turn fully exits.
        db.execute("UPDATE agent_workers SET state='running' WHERE id=?", (items[1]["worker_id"],))
    response = client.post(route, headers=headers, json={"items": selected, "decision": "approve"})
    assert response.status_code == 200, response.text
    batch_id = response.json()["batch_id"]
    assert {item["state"] for item in response.json()["items"]} == {"approved"}
    assert store.get_agent_worker(items[0]["worker_id"])["resume_mode"] == "same_session"
    assert store.get_agent_worker(items[0]["worker_id"])["state"] == "queued"
    assert store.get_agent_worker(items[1]["worker_id"])["state"] == "running"
    assert manager["instance"].schedule_calls == 1
    assert client.post(route, headers=headers, json={"items": selected, "decision": "approve"}).status_code == 409
    with store.connect() as db:
        events = [json.loads(r[0]) for r in db.execute("SELECT payload FROM events WHERE kind='agent.inbox_decided'")]
        assert len(events) == 2 and all(e["batch_id"] == batch_id for e in events)
        assert db.execute("SELECT COUNT(*) FROM events WHERE kind='agent.inbox_batch_decided'").fetchone()[0] == 1


@pytest.mark.parametrize("change", ["terms", "profile", "jd", "artifact", "stopped", "resolved", "first_review", "recovery", "wrong_pipeline"])
def test_one_invalid_request_rolls_back_entire_batch(tmp_path, change):
    client, headers, manager, store, pid, items, selected = fixture(tmp_path)
    target = items[-1]
    with store.connect(write=True) as db:
        if change in {"terms", "artifact"}:
            context = target["context"].copy()
            context["exact_terms" if change == "terms" else "ready_artifacts"] = "changed"
            db.execute("UPDATE agent_inbox SET context=? WHERE id=?", (json.dumps(context), target["id"]))
            if change == "artifact":
                # Even a newly fetched token cannot override the current artifact snapshot guard.
                selected[-1]["scope_token"] = store._agent_approval_scope_token({**target, "context": context})
        elif change == "profile":
            db.execute("UPDATE meta SET value=CAST(value AS INTEGER)+1 WHERE key='profile_version'")
        elif change == "jd":
            db.execute("UPDATE jobs SET jd_version=jd_version+1 WHERE id=?", (target["job_id"],))
        elif change == "stopped":
            db.execute("UPDATE pipelines SET stop_requested=1,status='stopped' WHERE id=?", (pid,))
        elif change == "resolved":
            db.execute("UPDATE agent_inbox SET state='approved' WHERE id=?", (target["id"],))
        elif change == "first_review":
            db.execute("UPDATE agent_inbox SET kind='first_application_review' WHERE id=?", (target["id"],))
        elif change == "recovery":
            db.execute("UPDATE agent_inbox SET action='browser_circuit_recovery' WHERE id=?", (target["id"],))
        elif change == "wrong_pipeline":
            selected[-1]["id"] = 'a-nonmember-checkpoint'
    response = client.post(f"/api/pipelines/{pid}/inbox/batch-decision", headers=headers,
                           json={"items": selected, "decision": "approve"})
    assert response.status_code == 409, response.text
    assert store.get_agent_worker(items[0]["worker_id"])["state"] == "waiting_approval"
    assert next(i for i in store.agent_inbox(pid) if i["id"] == items[0]["id"])["state"] == "pending"
    assert manager["instance"].schedule_calls == 0
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM events WHERE kind IN ('agent.inbox_decided','agent.inbox_batch_decided')").fetchone()[0] == 0


def test_selection_excludes_future_requests_and_duplicate_ids_are_rejected(tmp_path):
    client, headers, _, store, pid, items, selected = fixture(tmp_path)
    route = f"/api/pipelines/{pid}/inbox/batch-decision"
    assert client.post(route, headers=headers, json={"items": [selected[0]] * 2, "decision": "approve"}).status_code == 422
    future_id, _ = _seed_inbox(store, pid, items[0]["job_id"], "approval", action="another_action")
    response = client.post(route, headers=headers, json={"items": selected, "decision": "reject"})
    assert response.status_code == 200, response.text
    assert next(i for i in store.agent_inbox(pid) if i["id"] == future_id)["state"] == "pending"


def test_concurrent_batch_double_click_records_only_one_decision(tmp_path):
    _, _, _, store, pid, _, selected = fixture(tmp_path)
    barrier = threading.Barrier(2)
    def decide():
        barrier.wait()
        try:
            store.decide_agent_inbox_batch(pid, selected, "approve")
            return "saved"
        except Conflict:
            return "conflict"
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: decide(), range(2))) == ["conflict", "saved"]


def test_single_decision_can_also_guard_displayed_terms(tmp_path):
    client, headers, _, store, pid, items, selected = fixture(tmp_path)
    with store.connect(write=True) as db:
        db.execute("UPDATE agent_inbox SET summary='Changed exact action' WHERE id=?", (items[0]["id"],))
    response = client.post(f"/api/pipelines/{pid}/inbox/{items[0]['id']}/decision", headers=headers,
                           json={"decision": "approve", "scope_token": selected[0]["scope_token"]})
    assert response.status_code == 409


def test_current_browser_recovery_can_share_a_batch_but_superseded_recovery_cannot(tmp_path):
    client, headers, _, store, pid, items, _ = fixture(tmp_path)
    target = items[-1]
    with store.connect(write=True) as db:
        db.execute("UPDATE agent_inbox SET action='browser_circuit_recovery',context=? WHERE id=?",
                   (json.dumps({"scope": "connector:cua_repl", "source_event_id": "fictional-browser-event"}), target["id"]))
        db.execute("INSERT INTO agent_browser_controls(scope_key,control_type,state,worker_id,event_ids,updated_at) "
                   "VALUES('connector:cua_repl','connector_circuit','open',?,?,?)",
                   (target["worker_id"], json.dumps(["fictional-browser-event"]), timestamp()))
    current = store.agent_inbox(pid)
    assert all(i["batch_eligible"] for i in current)
    selected = [{"id": i["id"], "scope_token": i["scope_token"]} for i in current]
    route = f"/api/pipelines/{pid}/inbox/batch-decision"
    with store.connect(write=True) as db:
        db.execute("UPDATE agent_browser_controls SET state='closed'")
    assert client.post(route, headers=headers, json={"items": selected, "decision": "approve"}).status_code == 409
    assert all(i["state"] == "pending" for i in store.agent_inbox(pid))
    with store.connect(write=True) as db:
        db.execute("UPDATE agent_browser_controls SET state='open'")
    assert client.post(route, headers=headers, json={"items": selected, "decision": "approve"}).status_code == 200

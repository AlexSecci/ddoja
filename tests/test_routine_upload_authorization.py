from pathlib import Path
import hashlib
import json

import pytest

from app.store import Store, Conflict
from tools.agent_mcp import AgentMCPBridge, render_and_register_documents
from test_efficiency_controls import seed, start
from test_agent_renderer_integration import _make_document_child


def _approved_parent(tmp_path):
    store, pipeline, discovery, discovery_launch, _ = seed(tmp_path, 1)
    evaluator = next(w for w in store.list_agent_workers(pipeline["id"]) if w["role"] == "evaluator")
    launch = start(store, evaluator)
    result = store.agent_action(evaluator["id"], launch["capability"], "finish_evaluation", {"decision": "approve", "reason": "Fictional match"})
    store.finish_agent_turn(evaluator["id"], {"disposition": "completed"})
    parent = store.get_agent_worker(result["submission_worker_id"])
    parent_launch = start(store, parent)
    return store, pipeline, parent, parent_launch, discovery, discovery_launch


def test_standing_upload_permission_is_explicit_persistent_scoped_and_not_final_approval(tmp_path):
    store, pipeline, parent, launch, discovery, discovery_launch = _approved_parent(tmp_path)
    context = store.agent_action(parent["id"], launch["capability"], "get_context", {})
    assert context["runtime_controls"]["routine_document_uploads"]["enabled"] is False
    store.set_routine_document_uploads(True, "User authorized unattended tailored resume/cover-letter uploads to selected employers.")
    assert Store(tmp_path).routine_document_uploads()["enabled"] is True
    context = store.agent_action(parent["id"], launch["capability"], "get_context", {"sections": ["submission_state"]})
    policy = context["runtime_controls"]["routine_document_uploads"]
    assert policy["authorization_type"] == "user_standing_instruction"
    assert policy["company_id"] == pipeline["company_id"] and policy["job_id"] == parent["job_id"]
    assert policy["allowed_hosts"] == ["fictional.invalid"]
    assert policy["artifact_kinds"] == ["resume", "cover_letter"]
    assert context["sections"]["submission_state"]["current_approved_actions"] == []
    assert not store.agent_inbox(pipeline["id"])
    with pytest.raises(Conflict, match="first application"):
        store.agent_action(parent["id"], launch["capability"], "begin_final_action", {})
    assert "routine_document_uploads" not in store.agent_action(discovery["id"], discovery_launch["capability"], "get_context", {})["runtime_controls"]
    with pytest.raises(Conflict, match="not allowed"):
        store.agent_action(discovery["id"], discovery_launch["capability"], "set_routine_document_uploads", {"enabled": True})


@pytest.mark.parametrize("blocker", ["deselected", "unapproved", "stale_profile", "rejected"])
def test_standing_permission_never_overrides_job_scope_snapshot_or_user_rejection(tmp_path, blocker):
    store, pipeline, parent, launch, _, _ = _approved_parent(tmp_path)
    store.set_routine_document_uploads(True, "User instruction for routine uploads.")
    with store.connect(write=True) as db:
        if blocker == "deselected":
            db.execute("UPDATE pipeline_jobs SET selected=0 WHERE pipeline_id=?", (pipeline["id"],))
        elif blocker == "unapproved":
            db.execute("UPDATE pipeline_jobs SET agent_decision='discard' WHERE pipeline_id=?", (pipeline["id"],))
        elif blocker == "stale_profile":
            store.bump_profile(db)
        else:
            worker = db.execute("SELECT * FROM agent_workers WHERE id=?", (parent["id"],)).fetchone()
            item, _ = store._agent_insert_inbox(db, worker, "approval", action="exact_upload", context={
                "jd_version": parent["jd_version"], "profile_version": parent["profile_version"]})
            db.execute("UPDATE agent_inbox SET state='rejected' WHERE id=?", (item["id"],))
            db.execute("UPDATE agent_workers SET state='running' WHERE id=?", (parent["id"],))
    context = store.agent_action(parent["id"], launch["capability"], "get_context", {})
    assert context["runtime_controls"]["routine_document_uploads"]["enabled"] is False


def test_ready_upload_authorization_is_bound_to_the_actual_current_pdf_and_disappears_after_tampering(tmp_path):
    store = Store(tmp_path)
    store.set_routine_document_uploads(True, "User authorized routine resumes for selected employers.")
    pipeline, parent, child, launch = _make_document_child(store)
    bridge = AgentMCPBridge(child["id"], launch["capability"], "document", store.agent_action,
                            lambda value: render_and_register_documents(store, child["id"], launch["capability"], value))
    bridge.invoke("publish_documents", {"artifacts": [{"kind": "resume", "markdown": "# Test Candidate\n\n## Experience\n\n- Fictional C++ systems.\n"}]})
    store.finish_agent_turn(child["id"], {"disposition": "completed"})
    assert store.reserve_agent_worker(parent["id"])
    resumed = store.agent_launch_context(parent["id"])
    handles = resumed["task"]["task_context"]["prepared_document_handles"]
    assert len(handles) == 1
    handle = handles[0]
    auth = handle["upload_authorization"]
    assert auth["allowed_hosts"] == ["fixture.example"]
    assert auth["artifact_id"] == handle["artifact_id"]
    assert auth["upload_path"] == handle["upload_path"]
    assert auth["upload_sha256"] == hashlib.sha256(Path(handle["upload_path"]).read_bytes()).hexdigest()
    assert auth["jd_version"] == parent["jd_version"] and auth["profile_version"] == parent["profile_version"]
    Path(handle["upload_path"]).write_bytes(b"tampered file")
    context = store.agent_action(parent["id"], resumed["capability"], "get_context", {"sections": ["prepared_document_handles"]})
    assert context["sections"]["prepared_document_handles"] == []


def test_reviewed_upload_prompt_is_superseded_without_faking_approval_or_touching_legal_terms(tmp_path):
    store = Store(tmp_path)
    store.set_routine_document_uploads(True, "User requested unattended routine document uploads.")
    pipeline, parent, child, launch = _make_document_child(store)
    bridge = AgentMCPBridge(child["id"], launch["capability"], "document", store.agent_action,
                            lambda value: render_and_register_documents(store, child["id"], launch["capability"], value))
    bridge.invoke("publish_documents", {"artifacts": [{"kind": "resume", "markdown": "# Test Candidate\n\n## Experience\n\n- Fictional software.\n"}]})
    store.finish_agent_turn(child["id"], {"disposition": "completed"})
    with store.connect(write=True) as db:
        worker = db.execute("SELECT * FROM agent_workers WHERE id=?", (parent["id"],)).fetchone()
        ready = store._agent_artifact_snapshot(store._agent_ready_artifacts(db, pipeline["id"], parent["job_id"], parent["jd_version"], parent["profile_version"]))
        upload, _ = store._agent_insert_inbox(db, worker, "approval", action="upload", context={"page_url": "https://fixture.example/apply", "ready_artifacts": ready})
        legal, _ = store._agent_insert_inbox(db, worker, "approval", action="binding_agreement", context={"page_url": "https://fixture.example/apply"})
        db.execute("UPDATE agent_workers SET turn_active=0 WHERE id=?", (parent["id"],))
    with pytest.raises(Conflict):
        store.supersede_routine_upload_requests([upload["id"], legal["id"]])
    assert all(item["state"] == "pending" for item in store.agent_inbox(pipeline["id"]))
    with store.connect(write=True) as db:
        changed = {"page_url": "https://fixture.example.attacker.invalid/apply", "ready_artifacts": ready}
        db.execute("UPDATE agent_inbox SET context=? WHERE id=?", (json.dumps(changed), upload["id"]))
    with pytest.raises(Conflict, match="outside"):
        store.supersede_routine_upload_requests([upload["id"]])
    with store.connect(write=True) as db:
        changed["page_url"] = "https://fixture.example/apply"
        db.execute("UPDATE agent_inbox SET context=? WHERE id=?", (json.dumps(changed), upload["id"]))
    result = store.supersede_routine_upload_requests([upload["id"]])
    assert result == {"superseded": 1, "per_file_human_approvals_recorded": 0}
    items = {item["id"]: item for item in store.agent_inbox(pipeline["id"])}
    assert items[upload["id"]]["state"] == "stale" and items[upload["id"]]["response"] == ""
    assert items[legal["id"]]["state"] == "pending"
    assert store.get_agent_worker(parent["id"])["state"] == "waiting_approval"

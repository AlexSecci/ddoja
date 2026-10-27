import json
from pathlib import Path

import pytest

from app.agent_runtime import CodexAgentRuntime
from app.store import Conflict
from test_efficiency_controls import seed, start, cua


def test_compact_index_preserves_depth_and_exact_retrieval_in_controls_call(tmp_path):
    store, pipeline, _, _, _ = seed(tmp_path, 1)
    doc = tmp_path / "evidence.md"
    doc.write_text("# Experience\nExact detail, never truncate me.\n## CUDA\nStudied only.\n")
    store.import_document("evidence", doc)
    with store.connect(write=True) as db:
        db.execute("UPDATE agent_workers SET profile_version=? WHERE role='evaluator'", (store.profile_version(db),))
        db.execute("INSERT INTO technologies(key,name,answer,context,source,updated_at) VALUES(?,?,?,?,?,?)",
                   ("cuda", "CUDA", "yes", "Studied only; no production", "Exact applicant provenance", "2026-10-25"))
    w = next(w for w in store.list_agent_workers(pipeline["id"]) if w["role"] == "evaluator")
    launch = start(store, w)
    idx = launch["task"]["task_context"]["applicant_index"]
    assert set(idx["source_documents"][0]["sections"][0]) == {"heading", "ref"}
    assert idx["confirmed_knowledge"]["cuda"]["context"] == "Studied only; no production"
    refs = [idx["source_documents"][0]["sections"][0]["ref"], "technology:cuda", "knowledge_aliases"]
    result = store.agent_action(w["id"], launch["capability"], "get_context", {"source_ids": refs})
    assert "runtime_controls" in result and result["sections"] == {}
    assert "never truncate me" in result["sources"][0]["content"]
    assert result["sources"][1]["source"] == "Exact applicant provenance"
    assert "aliases" in result["sources"][2]
    discovery = next(w for w in store.list_agent_workers(pipeline["id"]) if w["role"] == "discovery")
    with store.connect() as db:
        raw = db.execute("SELECT * FROM agent_workers WHERE id=?", (discovery["id"],)).fetchone()
        with pytest.raises(Conflict, match="discovery cannot retrieve"):
            store._agent_retrieve_sources(db, raw, {"source_ids": refs})


def test_backpressure_buffers_and_wakes_same_worker_without_model_polling(tmp_path):
    store, pipeline, _, _, _ = seed(tmp_path, 8)
    workers = [w for w in store.list_agent_workers(pipeline["id"]) if w["role"] == "evaluator"]
    for w in workers[:6]:
        launch = start(store, w)
        store.agent_action(w["id"], launch["capability"], "finish_evaluation", {"decision": "approve", "reason": "Fixture fit"})
        store.finish_agent_turn(w["id"], {"disposition": "completed"})
    assert not store.reserve_agent_worker(workers[6]["id"])
    assert store.get_agent_worker(workers[6]["id"])["state"] == "queued"
    assert any(w["worker_id"] == workers[6]["id"] for w in store.agent_runtime_status(pipeline["id"])["capacity_waits"])
    parent = next(w for w in store.list_agent_workers(pipeline["id"]) if w["role"] == "submission")
    assert store.reserve_agent_worker(parent["id"])
    with store.connect(write=True) as db:
        db.execute("UPDATE agent_workers SET state='discarded',turn_active=0 WHERE id=?", (parent["id"],))
    assert store.reserve_agent_worker(workers[6]["id"])


def test_circuit_limits_buffer_and_reports_orphan_without_claiming_recovery(tmp_path):
    store, pipeline, d, launch, _ = seed(tmp_path, 5)
    evaluators = [w for w in store.list_agent_workers(pipeline["id"]) if w["role"] == "evaluator"]
    for w in evaluators[:3]:
        ev = start(store, w)
        store.agent_action(w["id"], ev["capability"], "finish_evaluation", {"decision": "approve", "reason": "Fixture fit"})
        store.finish_agent_turn(w["id"], {"disposition": "completed"})
    cua(store, d, success=False)
    store.agent_action(d["id"], launch["capability"], "browser_control", {"operation": "open_circuit", "reason": "Connector failed"})
    store.finish_agent_turn(d["id"], {"disposition": "failed", "error": "Connector failed"})
    assert not store.reserve_agent_worker(evaluators[3]["id"])
    with store.connect(write=True) as db:
        store._agent_refresh_pipeline(db, pipeline["id"])
    report = store.get_pipeline(pipeline["id"])
    assert report["stage"] == "browser_gate" and report["blockers"]
    assert store.agent_runtime_status(pipeline["id"])["circuits"][0]["open"]


def test_recovery_owner_can_drain_full_buffer_after_approval(tmp_path):
    store, pipeline, d, launch, _ = seed(tmp_path, 18)
    workers = [w for w in store.list_agent_workers(pipeline["id"]) if w["role"] == "evaluator"]
    for w in workers[:6]:
        ev = start(store, w)
        store.agent_action(w["id"], ev["capability"], "finish_evaluation", {"decision": "approve", "reason": "Fixture fit"})
        store.finish_agent_turn(w["id"], {"disposition": "completed"})
    cua(store, d, success=False)
    gate = store.agent_action(d["id"], launch["capability"], "browser_control", {"operation": "open_circuit", "reason": "Fixture failure"})
    store.finish_agent_turn(d["id"], {"disposition": "waiting"})
    assert not store.reserve_agent_worker(d["id"])  # Still respects pending approval.
    store.decide_agent_inbox(gate["inbox_item_id"], "approve", "Synthetic recovery approved")
    assert store.reserve_agent_worker(d["id"])
    assert not store.reserve_agent_worker(workers[6]["id"])  # No upstream flood.


def test_isolate_tab_timeout_preserves_job_approval_and_requires_independent_evidence(tmp_path):
    store, pipeline, _, _, _ = seed(tmp_path, 2)
    for evaluator in [w for w in store.list_agent_workers(pipeline["id"]) if w["role"] == "evaluator"]:
        launch = start(store, evaluator)
        store.agent_action(evaluator["id"], launch["capability"], "finish_evaluation", {"decision": "approve", "reason": "Fixture"})
        store.finish_agent_turn(evaluator["id"], {"disposition": "completed"})
    owner, other = [w for w in store.list_agent_workers(pipeline["id"]) if w["role"] == "submission"]
    launch = start(store, owner)
    start(store, other)
    cua(store, owner, success=False, text="Timed out after 10000ms waiting for CDP command Page.getFrameTree.")
    gate = store.agent_action(owner["id"], launch["capability"], "browser_control", {"operation": "open_circuit"})
    store.finish_agent_turn(owner["id"], {"disposition": "waiting"})
    with pytest.raises(Conflict):
        store.isolate_tab_navigation_timeout(owner["id"], "not-an-observation")
    job = store.get_job(other["job_id"])
    event = cua(store, other, text=f'Browser tab: 7, Title: "Apply", URL: "https://fictional.invalid/{job["requisition_id"]}/apply"\n0 AXWebArea Apply')
    result = store.isolate_tab_navigation_timeout(owner["id"], event)
    assert result["connector_state"] == "closed" and result["job_retry_authorized"] is False
    assert store.get_agent_worker(owner["id"])["state"] == "waiting_approval"
    assert next(i for i in store.agent_inbox(pipeline["id"]) if i["id"] == gate["inbox_item_id"])["state"] == "pending"
    scoped = next(i for i in store.agent_inbox(pipeline["id"]) if i["id"] == gate["inbox_item_id"])
    assert scoped["action"] == "browser_tab_recovery" and scoped["batch_eligible"]
    assert scoped["context"]["scope"] == "job:" + owner["job_id"]
    store.decide_agent_inbox(scoped["id"], "approve")
    assert store.get_agent_worker(owner["id"])["context"]["tab_recovery"]["approval_item_id"] == scoped["id"]


def test_stable_evidence_prefix_and_native_compaction_defaults_are_preserved(tmp_path):
    (tmp_path/"prompts").mkdir()
    (tmp_path/"config").mkdir()
    (tmp_path/"config/workers.json").write_text('{"authentication":"chatgpt","models":{"evaluation":{"model":"gpt-6.1-sol"}}}')
    (tmp_path/"prompts/agent_core.md").write_text("Core truth and scope.")
    (tmp_path/"prompts/evaluation_agent.md").write_text("Evaluate one JD.")
    runtime = CodexAgentRuntime(tmp_path, codex_bin="fixture")
    def prompt(worker_id, base="current-base"):
        w = {"id": worker_id, "role": "evaluator"}
        return runtime._prompt({"worker": w, "task_context": {"applicant_index": {"same": "evidence", "templates": {"base": base}},
                               "context_revision": 1, "job": {"jd": worker_id}}}, w)
    a, b = prompt("volatile-a"), prompt("volatile-b", "new-base-after-child-finished")
    assert a.split("DURABLE WORKER STATE")[0] == b.split("DURABLE WORKER STATE")[0]
    assert a.count('"same":"evidence"') == 1
    assert '"document_templates":{"base":"current-base"}' in a
    assert '"document_templates":{"base":"new-base-after-child-finished"}' in b
    args = runtime._args({"id": "w", "role": "evaluator"}, Path("schema.json"), {})
    assert not any(a.startswith("model_auto_compact_token_limit=") for a in args)


def test_reobserving_jd_preserves_version_metadata_and_inflight_assessment(tmp_path):
    store, pipeline, d, launch, listings = seed(tmp_path, 1)
    recorded, payload = listings[0]
    w = next(w for w in store.list_agent_workers(pipeline["id"]) if w["role"] == "evaluator")
    ev = start(store, w)
    fresh = store.agent_action(w["id"], ev["capability"], "update_job_description", {
        "jd": payload["jd"], "observation": {"new_page_observation": True}})
    assert fresh["changed"] is False and fresh["jd_version"] == 1
    improved_jd = payload["jd"] + " More actual employer requirements."
    fixed = store.agent_action(w["id"], ev["capability"], "update_job_description", {"jd": improved_jd})
    assert fixed["changed"] and fixed["jd_version"] == 2
    with store.connect() as db:
        metadata = json.loads(db.execute("SELECT metadata FROM job_versions WHERE job_id=? AND version=2", (recorded["job_id"],)).fetchone()[0])
    assert metadata == payload["metadata"]
    same = store.agent_action(d["id"], launch["capability"], "record_listing", {**payload, "jd": improved_jd})
    assert same["changed"] is False and same["jd_version"] == 2
    # The same evaluator can commit without a wasted fresh assessment.
    committed = store.agent_action(w["id"], ev["capability"], "finish_evaluation", {"decision": "approve", "reason": "Current JD reviewed"})
    assert committed["decision"] == "approve"

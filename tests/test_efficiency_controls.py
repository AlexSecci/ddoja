from pathlib import Path
import json
import pytest

from app.store import Conflict, Store
from tools.agent_mcp import AgentMCPBridge, render_and_register_documents


def seed(tmp_path, count=2):
    store = Store(tmp_path)
    pipeline = store.create_agent_pipeline("Synthetic Efficiency", "https://fictional.invalid/careers")
    discovery = store.list_agent_workers(pipeline["id"])[0]
    launch = start(store, discovery)
    listings = []
    for index in range(count):
        payload = {"requisition_id": str(index), "url": f"https://fictional.invalid/job/{index}", "title": "Fictional C++ role",
                   "jd": "Full fictional description. C++ or Rust; distributed systems preferred.", "selected": True,
                   "metadata": {"country": "US", "employment": "full_time", "arrangement": "hybrid", "location": "Austin"}}
        result = store.agent_action(discovery["id"], launch["capability"], "record_listing", payload)
        listings.append((result, payload))
    return store, pipeline, discovery, launch, listings


def start(store, worker):
    assert store.reserve_agent_worker(worker["id"])
    launch = store.agent_launch_context(worker["id"])
    session = worker.get("session_id") or "fixture-" + worker["id"]
    store.agent_runtime_event(worker["id"], {"type": "thread.started", "thread_id": session})
    return launch


def cua(store, worker, success=True, text="Observed fictional page"):
    session = store.get_agent_worker(worker["id"])["session_id"]
    return store.agent_runtime_event(worker["id"], {"type": "tool_result", "tool_name": "cua_repl.js",
        "success": success, "session_id": session, "transport_event": {"type": "item.completed", "item": {
            "type": "mcp_tool_call", "server": "cua_repl", "tool": "js", "status": "completed" if success else "failed",
            "result": {"isError": not success, "content": [{"type": "text", "text": text}]}}}})


def test_sources_are_complete_retrievable_and_changed_section_handles_fail_closed(tmp_path):
    store, pipeline, _, _, _ = seed(tmp_path)
    source = tmp_path / "profile.md"
    content = "Preamble\n" + "\n".join(f"## Skill {i}\nEvidence {i}; studied only, never production.\n" for i in range(95))
    source.write_text(content)
    store.import_document("profile", source)
    worker = next(w for w in store.list_agent_workers(pipeline["id"]) if w["role"] == "evaluator")
    # Imported profile requires a fresh evaluator snapshot in this fixture.
    with store.connect(write=True) as db:
        db.execute("UPDATE agent_workers SET profile_version=? WHERE id=?", (store.profile_version(db), worker["id"]))
    launch = start(store, worker)
    index = launch["task"]["task_context"]["applicant_index"]
    sections = index["source_documents"][0]["sections"]
    assert len(sections) == 96 and "content" not in index["source_documents"][0]
    restored = []
    for section in sections:
        value = store.agent_action(worker["id"], launch["capability"], "retrieve_sources", {"source_ids": [section["ref"]]})
        restored.append(value["sources"][0]["content"])
    assert "".join(restored) == content
    source.write_text(content + "Correction.")
    store.import_document("profile", source)
    with pytest.raises(Conflict, match="stale"):
        store.agent_action(worker["id"], launch["capability"], "retrieve_sources", {"source_ids": [sections[0]["ref"]]})


def test_captured_jd_is_exact_and_batch_failure_rolls_back_and_duplicates_do_not_queue_twice(tmp_path):
    store, pipeline, discovery, launch, listings = seed(tmp_path, 0)
    text = "Metadata\nBEGIN JD\nC++ experience with MPI.\nEND JD\nFooter"
    cua(store, discovery, text=text)
    item = {"url": "https://fictional.invalid/1", "title": "Fictional engineer", "requisition_id": "1", "selected": True,
            "description_pointer": {"text_block": 0, "start_marker": "BEGIN JD", "end_marker": "END JD"}}
    bad = {**item, "url": "https://fictional.invalid/2", "requisition_id": "2", "description_pointer": {"text_block": 9}}
    with pytest.raises(ValueError):
        store.agent_action(discovery["id"], launch["capability"], "capture_listing_batch", {"listings": [item, bad]})
    assert store.pipeline_jobs(pipeline["id"])["jobs"] == []
    value = store.agent_action(discovery["id"], launch["capability"], "capture_listing_batch", {"listings": [item]})["recorded"][0]
    evaluator = next(w for w in store.list_agent_workers(pipeline["id"]) if w["role"] == "evaluator")
    assert start(store, evaluator)["task"]["task_context"]["job"]["jd"] == "BEGIN JD\nC++ experience with MPI.\nEND JD"
    again = store.agent_action(discovery["id"], launch["capability"], "capture_listing_batch", {"listings": [item]})["recorded"][0]
    assert not again["created"] and not again["changed"]
    assert len([w for w in store.list_agent_workers(pipeline["id"]) if w["role"] == "evaluator"]) == 1


def test_exact_shared_fact_key_resumes_subscribers_and_does_not_equate_technologies(tmp_path):
    store, pipeline, _, _, _ = seed(tmp_path, 3)
    evaluators = [w for w in store.list_agent_workers(pipeline["id"]) if w["role"] == "evaluator"]
    questions = []
    for i, worker in enumerate(evaluators):
        launch = start(store, worker)
        questions.append(store.agent_action(worker["id"], launch["capability"], "ask_question", {
            "question": "Know Flink?" if i < 2 else "Know Spark?", "fact_key": "knowledge_flink" if i < 2 else "knowledge_spark",
            "fact_scope": "applicant"}))
        store.finish_agent_turn(worker["id"], {"disposition": "waiting"})
    assert questions[0]["inbox_item_id"] == questions[1]["inbox_item_id"] != questions[2]["inbox_item_id"]
    store.answer_agent_inbox(questions[0]["inbox_item_id"], "Studied only; never used professionally", "Synthetic user answer")
    assert [store.get_agent_worker(w["id"])["state"] for w in evaluators] == ["queued", "queued", "waiting_question"]
    launch = start(store, store.get_agent_worker(evaluators[1]["id"]))
    inbox = store.agent_action(evaluators[1]["id"], launch["capability"], "get_context", {"sections": ["inbox"]})["sections"]["inbox"]
    assert inbox[0]["response"] == "Studied only; never used professionally"


def test_company_login_has_one_owner_and_offline_evaluation_keeps_running(tmp_path):
    store, pipeline, _, _, _ = seed(tmp_path, 3)
    workers = [w for w in store.list_agent_workers(pipeline["id"]) if w["role"] == "evaluator"]
    a, b = start(store, workers[0]), start(store, workers[1])
    cua(store, workers[0])
    assert store.agent_action(workers[0]["id"], a["capability"], "browser_control", {"operation": "acquire_auth"})["state"] == "owned"
    assert store.agent_action(workers[1]["id"], b["capability"], "browser_control", {"operation": "acquire_auth"})["state"] == "waiting_browser"
    store.finish_agent_turn(workers[1]["id"], {"disposition": "waiting"})
    assert store.get_agent_worker(workers[1]["id"])["state"] == "waiting_browser"
    assert store.reserve_agent_worker(workers[2]["id"])
    with pytest.raises(Conflict, match="new successful"):
        store.agent_action(workers[0]["id"], a["capability"], "browser_control", {"operation": "complete_auth"})
    cua(store, workers[0], text="Intended account now observed")
    store.agent_action(workers[0]["id"], a["capability"], "browser_control", {"operation": "complete_auth"})
    assert store.get_agent_worker(workers[1]["id"])["state"] == "queued"


def test_shared_connector_circuit_blocks_other_browser_work_and_requires_current_recovery(tmp_path):
    store, pipeline, d, launch, _ = seed(tmp_path, 1)
    other = store.create_agent_pipeline("Another fictional company", "https://other.invalid/jobs")
    other_d = store.list_agent_workers(other["id"])[0]
    cua(store, d, success=False)
    checkpoint = store.agent_action(d["id"], launch["capability"], "browser_control", {"operation": "open_circuit", "reason": "Actual synthetic connector failure"})
    store.finish_agent_turn(d["id"], {"disposition": "waiting"})
    assert not store.reserve_agent_worker(other_d["id"])
    evaluator = next(w for w in store.list_agent_workers(pipeline["id"]) if w["role"] == "evaluator")
    assert store.reserve_agent_worker(evaluator["id"])
    store.decide_agent_inbox(checkpoint["inbox_item_id"], "approve", "Synthetic recovery state changed")
    resumed = start(store, store.get_agent_worker(d["id"]))
    cua(store, d)
    store.agent_action(d["id"], resumed["capability"], "browser_control", {"operation": "complete_recovery"})
    assert store.reserve_agent_worker(other_d["id"])


def test_usage_deltas_are_idempotent_and_do_not_treat_unreported_caching_as_zero(tmp_path):
    store, pipeline, d, _, _ = seed(tmp_path, 0)
    base = {"session_id": "s", "turn_id": "1", "counter_kind": "cumulative", "counters": {
        "input_tokens": 100, "cached_input_tokens": 60, "output_tokens": 20, "reasoning_tokens": 8}}
    store.record_agent_usage(d["id"], base)
    assert store.record_agent_usage(d["id"], base)["duplicate"]
    store.record_agent_usage(d["id"], {**base, "turn_id": "2", "counters": {
        "input_tokens": 150, "cached_input_tokens": 80, "output_tokens": 30, "reasoning_tokens": 10}})
    store.record_agent_usage(d["id"], {"session_id": "other", "turn_id": "1", "counter_kind": "turn",
                                      "counters": {"input_tokens": 40, "output_tokens": 3}})
    totals = store.get_agent_efficiency(pipeline["id"])["totals"]
    assert totals["input_tokens"] == 190 and totals["cached_input_tokens"] == 80
    assert totals["uncached_input_tokens"] == 70 and totals["missing"]["uncached_input_tokens"] == 1
    assert totals["output_tokens"] == 33 and totals["reasoning_tokens"] == 10
    with pytest.raises(Conflict, match="decreased"):
        store.record_agent_usage(d["id"], {**base, "turn_id": "3"})


def test_reusable_plan_matches_exact_observed_structure_only(tmp_path):
    store, _, d, launch, _ = seed(tmp_path, 0)
    args = {"origin": "https://fictional.invalid", "structure_fingerprint": "a" * 64}
    with pytest.raises(Conflict):
        store.agent_action(d["id"], launch["capability"], "save_workflow_plan", {**args, "plan": {"fields": []}})
    cua(store, d)
    store.agent_action(d["id"], launch["capability"], "save_workflow_plan", {**args, "plan": {"fields": []}})
    assert store.agent_action(d["id"], launch["capability"], "lookup_workflow_plan", args)["found"]
    assert not store.agent_action(d["id"], launch["capability"], "lookup_workflow_plan", {**args, "structure_fingerprint": "b" * 64})["found"]


def test_evaluation_cache_invalidates_policy_profile_and_filter_changes(tmp_path):
    store, pipeline, d, launch, listings = seed(tmp_path, 1)
    (tmp_path / "prompts").mkdir()
    (tmp_path / "config").mkdir()
    (tmp_path / "prompts/evaluation_agent.md").write_text("Judge the full JD")
    (tmp_path / "config/workers.json").write_text(json.dumps({"models": {"evaluation": {"model": "gpt-6.1-sol", "effort": "high"}}}))
    evaluator = next(w for w in store.list_agent_workers(pipeline["id"]) if w["role"] == "evaluator")
    e = start(store, evaluator)
    store.agent_action(evaluator["id"], e["capability"], "finish_evaluation", {
        "score": 0, "eligibility": "blocked", "confidence": "high", "reason": "Synthetic confirmed barrier",
        "requirements": [{"requirement": "Fixture eligibility", "importance": "hard_eligibility", "match": "missing", "evidence": "Synthetic fixture"}],
        "hard_blockers": [{"requirement": "Fixture eligibility", "job_evidence": "Explicit fixture requirement", "applicant_evidence": "Confirmed fixture incompatibility"}]})
    store.finish_agent_turn(evaluator["id"], {"disposition": "completed"})
    store.agent_action(d["id"], launch["capability"], "finish_discovery", {"status": "complete", "detail": "Synthetic coverage"})
    store.finish_agent_turn(d["id"], {"disposition": "completed"})
    second = store.create_agent_pipeline("Synthetic Efficiency", "https://fictional.invalid/careers")
    d2 = store.list_agent_workers(second["id"])[0]
    a2 = start(store, d2)
    reused = store.agent_action(d2["id"], a2["capability"], "record_listing", listings[0][1])
    assert store.get_agent_worker(reused["evaluator_worker_id"])["result"]["cached"]
    with store.connect() as db:
        before = store._agent_evaluation_cache_snapshot(db, second["id"], reused["job_id"], reused["jd_version"])["cache_key"]
    (tmp_path / "prompts/agent_core.md").write_text("Changed mandatory policy")
    with store.connect() as db:
        changed_policy = store._agent_evaluation_cache_snapshot(db, second["id"], reused["job_id"], reused["jd_version"])["cache_key"]
        assert changed_policy != before
    source = tmp_path / "new-profile.md"
    source.write_text("# Candidate\nNew confirmed evidence.")
    store.import_document("master_resume", source)
    with store.connect() as db:
        changed_profile = store._agent_evaluation_cache_snapshot(db, second["id"], reused["job_id"], reused["jd_version"])["cache_key"]
        assert changed_profile != changed_policy
    with store.connect(write=True) as db:
        db.execute("UPDATE pipelines SET filters=? WHERE id=?", (json.dumps({"title_contains": "backend"}), second["id"]))
        assert store._agent_evaluation_cache_snapshot(db, second["id"], reused["job_id"], reused["jd_version"])["cache_key"] != changed_profile


def test_render_cache_and_verified_base_require_current_files_and_profile(tmp_path):
    from test_agent_renderer_integration import _make_document_child
    store = Store(tmp_path)
    pipeline, parent, child, launch = _make_document_child(store)
    bridge = AgentMCPBridge(child["id"], launch["capability"], "document", store.agent_action,
                            lambda value: render_and_register_documents(store, child["id"], launch["capability"], value))
    payload = {"artifacts": [{"kind": "resume", "markdown": "# Test Candidate\n\n## Experience\n\n- Fictional C++ software.\n"}]}
    one, images = bridge.invoke("publish_documents", payload)
    two, repeated_images = bridge.invoke("publish_documents", payload)
    assert images and not repeated_images and two["unchanged_preview_already_returned_this_turn"]
    assert store.get_agent_efficiency(pipeline["id"])["cache"]["render_hits"] >= 1
    artifact = two["artifacts"][0]
    base = bridge.call("save_resume_base", {"artifact_id": artifact["id"], "family": "Systems", "summary": "Synthetic reviewed base"})["ref"]
    with pytest.raises(Conflict):
        bridge.call("retrieve_sources", {"source_ids": [base]})
    store.finish_agent_turn(child["id"], {"disposition": "completed"})
    parent_launch = start(store, store.get_agent_worker(parent["id"]))
    value = store.agent_action(parent["id"], parent_launch["capability"], "retrieve_sources", {"source_ids": [base]})
    assert value["sources"][0]["content"] == payload["artifacts"][0]["markdown"]
    path = tmp_path / artifact["path"]
    path.write_text("Tampered")
    with pytest.raises(Conflict):
        store.agent_action(parent["id"], parent_launch["capability"], "retrieve_sources", {"source_ids": [base]})


def test_answer_before_turn_exit_accepts_only_the_active_session_and_resumes_with_deltas(tmp_path):
    store, pipeline, _, _, _ = seed(tmp_path, 1)
    worker = next(w for w in store.list_agent_workers(pipeline["id"]) if w["role"] == "evaluator")
    launch = start(store, worker)
    item = store.agent_action(worker["id"], launch["capability"], "ask_question", {
        "question": "Any hands-on fictional framework experience?", "fact_scope": "job"})
    store.answer_agent_inbox(item["inbox_item_id"], "Studied only", "Synthetic applicant answer")
    current = store.get_agent_worker(worker["id"])
    assert current["state"] == "queued" and current["turn_active"] == 1
    # The old process is still returning the tool result while the next turn is queued.
    store.agent_runtime_event(worker["id"], {"type": "tool_result", "tool_name": "agent_bridge.ask_question",
                                            "session_id": current["session_id"], "success": True})
    with pytest.raises(Conflict):
        store.agent_runtime_event(worker["id"], {"type": "tool_result", "session_id": "unrelated-session"})
    store.finish_agent_turn(worker["id"], {"disposition": "waiting"})
    with pytest.raises(Conflict):
        store.agent_runtime_event(worker["id"], {"type": "tool_result", "session_id": current["session_id"]})
    resumed = start(store, store.get_agent_worker(worker["id"]))
    packet = resumed["task"]["task_context"]
    assert "job" not in packet and "applicant_index" not in packet
    assert any(event["kind"] == "agent.inbox_answered" for event in packet["updates"]["events"])
    inbox = store.agent_action(worker["id"], resumed["capability"], "get_context", {"sections": ["inbox"]})
    assert inbox["sections"]["inbox"][0]["response"] == "Studied only"


def test_evaluator_commit_survives_transport_exit_and_reaches_submission(tmp_path):
    store, pipeline, _, _, _ = seed(tmp_path, 1)
    worker = next(w for w in store.list_agent_workers(pipeline["id"]) if w["role"] == "evaluator")
    launch = start(store, worker)
    committed = store.agent_action(worker["id"], launch["capability"], "finish_evaluation", {
        "decision": "approve", "reason": "C++ evidence fits; sponsorship remains unknown.",
        "observations": {"unknowns": ["Employer sponsorship"]}})
    store.finish_agent_turn(worker["id"], {"disposition": "completed", "summary": "Transport exit"})
    result = store.get_agent_worker(worker["id"])["result"]
    assert result["decision"] == "approve" and result["transport_result"]["summary"] == "Transport exit"
    submission = store.get_agent_worker(committed["submission_worker_id"])
    packet = start(store, submission)["task"]["task_context"]
    assert packet["job"]["evaluation_findings"]["observations"]["unknowns"] == ["Employer sponsorship"]
    # Recover old records whose transport previously overwrote the commit.
    with store.connect(write=True) as db:
        db.execute("UPDATE agent_workers SET result=? WHERE id=?", ('{"summary":"Historical transport"}', worker["id"]))
    with store.connect() as db:
        current = db.execute("SELECT * FROM agent_workers WHERE id=?", (submission["id"],)).fetchone()
        assert store._agent_current_job(db, current)["evaluation_findings"]["reason"] == committed["reason"]


def test_large_discovery_checkpoint_is_bounded_at_launch_but_exactly_retrievable(tmp_path):
    store = Store(tmp_path)
    pipeline = store.create_agent_pipeline("Fictional bounded history", "https://bounded.invalid/careers")
    worker = store.list_agent_workers(pipeline["id"])[0]
    history = "Full durable historical observation. " * 2500
    with store.connect(write=True) as db:
        db.execute("UPDATE agent_workers SET context=?,result=? WHERE id=?", (
            json.dumps({"current_page": 7, "historical_scan": history}), json.dumps({"old_result": history}), worker["id"]))
        db.execute("UPDATE pipelines SET discovery_payload=? WHERE id=?", (
            json.dumps({"coverage": {"current_page": 7, "large_history": [history]}}), pipeline["id"]))
    launch = start(store, worker)
    view = launch["task"]["worker"]
    assert "result" not in view and view["context"]["current_page"] == 7
    assert view["context"]["historical_scan"]["omitted_from_launch"]
    coverage = launch["task"]["task_context"]["pipeline"]["discovery"]["coverage"]
    assert coverage["current_page"] == 7 and coverage["large_history"]["omitted_from_launch"]
    packet = store.agent_action(worker["id"], launch["capability"], "get_context", {"sections": ["checkpoint"]})
    checkpoint = packet["sections"]["checkpoint"]
    assert checkpoint["context"]["historical_scan"] == history
    assert checkpoint["historical_result"]["old_result"] == history
    assert checkpoint["discovery_history"]["coverage"]["large_history"] == [history]


def test_human_wait_metrics_merge_overlapping_questions(tmp_path):
    store, pipeline, _, _, _ = seed(tmp_path, 2)
    for worker in [w for w in store.list_agent_workers(pipeline["id"]) if w["role"] == "evaluator"]:
        launch = start(store, worker)
        store.agent_action(worker["id"], launch["capability"], "ask_question", {"question": "Synthetic question?"})
    with store.connect(write=True) as db:
        rows = db.execute("SELECT id FROM agent_inbox ORDER BY id").fetchall()
        for row, seconds in zip(rows, (0, 5)):
            db.execute("UPDATE agent_inbox SET state='answered',created_at=?,resolved_at=? WHERE id=?", (
                f"2026-10-23T10:00:{seconds:02d}Z", f"2026-10-23T10:00:{seconds + 10:02d}Z", row["id"]))
    assert store.get_agent_efficiency(pipeline["id"])["human_wait_seconds"] == 15

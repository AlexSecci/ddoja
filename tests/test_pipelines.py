from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import json
from threading import Event
import time
from types import SimpleNamespace

import pytest

from app.pipelines import PipelineManager
from app.store import Conflict, Store, digest


def listing(req="R-1", **changes):
    return {"requisition_id": req, "url": f"https://jobs.example.test/{req}", "title": "Systems Engineer",
            "location": "Austin, TX", "country": "US", "employment": "full_time",
            "arrangement": "onsite", "jd": "Full-time systems engineering role in Austin.", **changes}


def configured_store(tmp_path, company_id="acme"):
    store = Store(tmp_path)
    store.company(company_id, "Acme Corp", "https://boards.greenhouse.io/acme", enabled=True)
    store.source(company_id, "greenhouse", "acme")
    return store


def _add_upload_package(store, job_id, package_id):
    manifest = {"upload_ready": True, "files": [], "pdf_files": []}
    with store.connect(write=True) as db:
        job = db.execute("SELECT jd_version FROM jobs WHERE id=?", (job_id,)).fetchone()
        db.execute("INSERT INTO packages VALUES(?,?,?,?,?,?)",
                   (package_id, job_id, job["jd_version"], store.profile_version(db), json.dumps(manifest), "2026-10-12T00:00:00+00:00"))


def _save_manual_auth_context(store, run_id, job_id, *, final_action_may_have_been_sent=False):
    package_id = f"auth-package-{job_id}"
    _add_upload_package(store, job_id, package_id)
    with store.connect(write=True) as db:
        job = db.execute("SELECT jd_version FROM jobs WHERE id=?", (job_id,)).fetchone()
        profile_version = store.profile_version(db)
        manifest_hash = digest(json.dumps({"files": [], "pdf_files": []}, sort_keys=True, ensure_ascii=False))
        context = {"manual_handoff": True}
        if final_action_may_have_been_sent:
            context["final_action_may_have_been_sent"] = True
        db.execute("UPDATE jobs SET phase='ready_for_submission',run_status='submission_blocked',"
                   "active_stage=NULL,lease_token=NULL,reason='Manual sign-in required.' WHERE id=?", (job_id,))
        db.execute("INSERT INTO submission_contexts(job_id,pipeline_id,session_id,tab_id,checkpoint,package_id,"
                   "jd_version,profile_version,package_sha256,context,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                   (job_id, run_id, "saved-session-1", "saved-tab-1", "manual_authentication", package_id,
                    job["jd_version"], profile_version, manifest_hash, json.dumps(context), "2026-10-12T00:00:00+00:00"))
        db.execute("UPDATE pipelines SET status='blocked',stage='submission',discovery_status='complete' WHERE id=?", (run_id,))


def test_unsupported_company_creates_persisted_blocker_not_completed_run(tmp_path):
    store = Store(tmp_path)
    store.company("custom", "Custom Careers", "https://careers.custom.test/jobs", enabled=True)

    run = store.create_pipeline("custom", title_contains="Platform")

    assert run["status"] == "blocked"
    assert run["discovery_status"] == "blocked"
    assert "No supported public job-board feed" in run["blockers"][0]
    assert run["counts"]["discovered"] == 0
    assert store.get_pipeline(run["id"])["status"] != "completed"


def test_filter_exclusions_are_run_local_and_unknown_location_is_evaluated(tmp_path):
    store = configured_store(tmp_path)
    run = store.create_pipeline("acme", title_contains="Platform", location_contains="Seattle")
    match = store.discover("acme", listing("R-1", title="Platform Engineer", location="Not specified"))
    wrong_title = store.discover("acme", listing("R-2", title="Support Engineer", location="Seattle, WA"))
    wrong_location = store.discover("acme", listing("R-3", title="Platform Engineer", location="Austin, TX"))
    store.add_pipeline_job(run["id"], match["id"])
    store.add_pipeline_job(run["id"], wrong_title["id"], selected=False, excluded_reason="Title does not match: Platform")
    store.add_pipeline_job(run["id"], wrong_location["id"], selected=False, excluded_reason="Location does not match: Seattle")

    rows = {job["id"]: job for job in store.pipeline_jobs(run["id"])["jobs"]}
    assert rows[match["id"]]["selected"] is True
    assert rows[match["id"]]["evaluation_status"] == "pending"
    assert rows[wrong_title["id"]]["excluded_reason"] == "Title does not match: Platform"
    assert rows[wrong_location["id"]]["selected"] is False
    assert all(store.get_job(job_id)["phase"] == "new" for job_id in (match["id"], wrong_title["id"], wrong_location["id"]))
    counts = store.get_pipeline(run["id"])["counts"]
    assert (counts["discovered"], counts["selected"], counts["excluded"]) == (3, 1, 2)


def test_manual_auth_acknowledgment_resumes_only_saved_current_tab_before_final_action(tmp_path):
    store = configured_store(tmp_path)
    run = store.create_pipeline("acme")
    job_id = store.discover("acme", listing())["id"]
    store.add_pipeline_job(run["id"], job_id)
    _save_manual_auth_context(store, run["id"], job_id)

    status = store.manual_auth_resume_status(job_id, run["id"])
    assert status == {"available": True, "checkpoint": "manual_authentication",
                      "note": "Sign-in can resume in the preserved employer tab."}
    assert "session" not in json.dumps(status) and "tab_id" not in json.dumps(status)

    store.acknowledge_manual_authentication(job_id, run["id"], "Signed in on the employer page.")
    store.activate_manual_authentication_resume(job_id, run["id"])
    job = store.get_job(job_id)
    saved = store.get_submission_context(job_id, run["id"])
    assert job["run_status"] == "idle"
    assert (saved["session_id"], saved["tab_id"]) == ("saved-session-1", "saved-tab-1")
    assert saved["checkpoint"] == "manual_authentication"
    assert saved["context"]["manual_handoff"] is True
    assert saved["context"]["manual_auth_acknowledged"] is False


def test_manual_auth_resume_waits_for_existing_pipeline_worker_to_exit(tmp_path):
    from concurrent.futures import Future

    store = configured_store(tmp_path)
    run = store.create_pipeline("acme")
    job_id = store.discover("acme", listing())["id"]
    store.add_pipeline_job(run["id"], job_id)
    _save_manual_auth_context(store, run["id"], job_id)
    manager = PipelineManager(store)
    prior_worker = Future()
    manager._futures[run["id"]] = prior_worker
    starts = []
    manager.start = lambda pipeline_id: starts.append(pipeline_id) or store.get_pipeline(pipeline_id)
    try:
        pending = manager.resume_manual_authentication(run["id"], job_id, "Signed in on employer page.")
        assert pending["status"] == "blocked"
        assert store.manual_auth_resume_status(job_id, run["id"])["available"] is True
        with store.connect() as db:
            assert db.execute("SELECT run_status FROM jobs WHERE id=?", (job_id,)).fetchone()[0] == "submission_blocked"

        prior_worker.set_result(None)

        assert store.get_pipeline(run["id"])["status"] == "queued"
        assert starts == [run["id"]]
        saved = store.get_submission_context(job_id, run["id"])
        assert (saved["session_id"], saved["tab_id"]) == ("saved-session-1", "saved-tab-1")
        with store.connect() as db:
            assert db.execute("SELECT run_status FROM jobs WHERE id=?", (job_id,)).fetchone()[0] == "idle"
    finally:
        manager.shutdown()


def test_manual_auth_and_discard_reject_possible_final_action_or_uncertainty(tmp_path):
    store = configured_store(tmp_path)
    run = store.create_pipeline("acme")
    final_job = store.discover("acme", listing("R-FINAL"))["id"]
    uncertain_job = store.discover("acme", listing("R-UNCERTAIN"))["id"]
    receipt_job = store.discover("acme", listing("R-RECEIPT"))["id"]
    store.add_pipeline_job(run["id"], final_job)
    store.add_pipeline_job(run["id"], uncertain_job)
    store.add_pipeline_job(run["id"], receipt_job)
    _save_manual_auth_context(store, run["id"], final_job, final_action_may_have_been_sent=True)
    _save_manual_auth_context(store, run["id"], uncertain_job)
    with store.connect(write=True) as db:
        db.execute("UPDATE jobs SET run_status='submission_uncertain' WHERE id=?", (uncertain_job,))
        db.execute("INSERT INTO evidence VALUES(?,?,?,?,?,?,?,?)",
                   ("receipt-evidence", receipt_job, "receipt", "fixture://receipt", "a" * 64,
                    "fixture", "synthetic receipt for test", "2026-10-12T00:00:00+00:00"))

    assert not store.manual_auth_resume_status(final_job, run["id"])["available"]
    assert "may already have been sent" in store.manual_auth_resume_status(final_job, run["id"])["note"]
    assert not store.manual_auth_resume_status(uncertain_job, run["id"])["available"]
    assert not store.manual_discard_status(final_job, run["id"])["available"]
    assert not store.manual_discard_status(uncertain_job, run["id"])["available"]
    assert not store.manual_discard_status(receipt_job, run["id"])["available"]
    with pytest.raises(Conflict, match="final action may already have been sent"):
        store.discard_pipeline_job(final_job, run["id"], "No longer interested", "User decision")
    with pytest.raises(Conflict, match="outcome is uncertain"):
        store.discard_pipeline_job(uncertain_job, run["id"], "No longer interested", "User decision")
    with pytest.raises(Conflict, match="receipt exists"):
        store.discard_pipeline_job(receipt_job, run["id"], "No longer interested", "User decision")


def test_manual_auth_resume_rechecks_jd_snapshot_after_user_acknowledgment(tmp_path):
    store = configured_store(tmp_path)
    run = store.create_pipeline("acme")
    job_id = store.discover("acme", listing())["id"]
    store.add_pipeline_job(run["id"], job_id)
    _save_manual_auth_context(store, run["id"], job_id)
    store.acknowledge_manual_authentication(job_id, run["id"], "Signed in on employer page.")
    with store.connect(write=True) as db:
        db.execute("UPDATE jobs SET jd_version=jd_version+1 WHERE id=?", (job_id,))

    with pytest.raises(Conflict, match="no longer safe to resume|package or applicant profile changed"):
        store.activate_manual_authentication_resume(job_id, run["id"])
    with store.connect() as db:
        assert db.execute("SELECT run_status FROM jobs WHERE id=?", (job_id,)).fetchone()[0] == "submission_blocked"


def test_manual_discard_is_pipeline_scoped_and_counts_as_terminal_without_mutating_job(tmp_path):
    store = configured_store(tmp_path)
    run = store.create_pipeline("acme")
    job_id = store.discover("acme", listing())["id"]
    store.add_pipeline_job(run["id"], job_id)
    _save_manual_auth_context(store, run["id"], job_id)

    assert store.manual_discard_status(job_id, run["id"])["available"] is True
    result = store.discard_pipeline_job(job_id, run["id"], "Role is no longer relevant", "Reviewed job page")
    assert result["reason"] == "Role is no longer relevant"
    assert store.get_job(job_id)["run_status"] == "submission_blocked"
    row = store.pipeline_jobs(run["id"])["jobs"][0]
    assert row["manual_discard_reason"] == "Role is no longer relevant"
    assert row["manual_discard"]["available"] is False
    counts = store.get_pipeline(run["id"])["counts"]
    assert counts["selected"] == counts["discarded"] == 1
    assert store.pipeline_submission_jobs(run["id"]) == []


def test_manual_discard_rejects_an_active_claim(tmp_path):
    store = configured_store(tmp_path)
    run = store.create_pipeline("acme")
    job_id = store.discover("acme", listing())["id"]
    store.add_pipeline_job(run["id"], job_id)
    store.update_pipeline(run["id"], status="running")
    claim = store.claim("evaluation", "test-worker", pipeline_id=run["id"])

    assert claim and claim["job_id"] == job_id
    assert store.manual_discard_status(job_id, run["id"])["available"] is False
    with pytest.raises(Conflict, match="worker still owns this job"):
        store.discard_pipeline_job(job_id, run["id"], "No longer relevant", "User decision")
    store.fail(claim, "cleanup")


def test_claims_are_company_scoped_and_durable_stop_blocks_next_claim(tmp_path):
    store = configured_store(tmp_path)
    store.company("other", "Other Corp", "https://boards.greenhouse.io/other", enabled=True)
    store.source("other", "greenhouse", "other")
    store.company("orphan", "Unmanaged Corp", "https://boards.greenhouse.io/orphan", enabled=True)
    run = store.create_pipeline("acme")
    other_run = store.create_pipeline("other")
    acme_job = store.discover("acme", listing("R-A"))
    other_job = store.discover("other", listing("R-B"))
    unmanaged_job = store.discover("orphan", listing("R-C"))
    store.add_pipeline_job(run["id"], acme_job["id"])
    store.add_pipeline_job(other_run["id"], other_job["id"])
    store.update_pipeline(run["id"], status="running")
    store.update_pipeline(other_run["id"], status="running")

    claim = store.claim("evaluation", "acme-worker", pipeline_id=run["id"])
    assert claim["job_id"] == acme_job["id"]
    with pytest.raises(Conflict, match="already has an active"):
        store.create_pipeline("acme")
    store.fail(claim, "test cleanup")

    manager = PipelineManager(store)
    try:
        stopped = manager.stop(run["id"])
    finally:
        manager.shutdown()
    assert stopped["status"] == "stopped" and stopped["stop_requested"] is True
    assert store.claim("evaluation", "acme-worker", pipeline_id=run["id"]) is None
    global_claim = store.claim("evaluation", "legacy-global-worker")
    assert global_claim and global_claim["job_id"] == unmanaged_job["id"]
    store.fail(global_claim, "test cleanup")
    other_claim = store.claim("evaluation", "other-worker", pipeline_id=other_run["id"])
    assert other_claim and other_claim["job_id"] == other_job["id"]


def test_recruiter_outcome_requires_verified_submission_and_unknown_is_preserved(tmp_path):
    store = configured_store(tmp_path)
    job = store.discover("acme", listing())["id"]
    with pytest.raises(Conflict, match="verified submitted"):
        store.record_application_outcome(job, "accepted", "Recruiter email")

    receipt = tmp_path / "receipt.md"
    receipt.write_text("Your Application Has Been Received. Req R-1", encoding="utf-8")
    store.import_submission(job, receipt, "receipt", "fixture")
    outcome = store.record_application_outcome(job, "unknown", "No recruiter response was visible.")
    assert outcome["application_status"] == "unknown"
    assert store.get_job(job)["application_outcome"]["status"] == "unknown"


def test_stale_pending_action_approval_rejects_changed_jd_snapshot(tmp_path):
    store = configured_store(tmp_path)
    run = store.create_pipeline("acme")
    job = store.discover("acme", listing())["id"]
    store.add_pipeline_job(run["id"], job)
    package_id = "reviewed-package"
    _add_upload_package(store, job, package_id)
    terms = "I consent to the employer's stated terms."
    from app.store import digest
    with store.connect(write=True) as db:
        db.execute("UPDATE jobs SET phase='ready_for_submission',run_status='awaiting_user' WHERE id=?", (job,))
        db.execute("INSERT INTO submission_approvals(id,job_id,pipeline_id,action,jd_version,profile_version,package_id,terms_hash,terms_text,status,created_at,updated_at) "
                   "VALUES(?,?,?,?,?,?,?,?,?,'pending',?,?)",
                   ("approval-1", job, run["id"], "terms", 1, store.profile_version(db), package_id,
                    digest(terms), terms, "2026-10-12T00:00:00+00:00", "2026-10-12T00:00:00+00:00"))
        db.execute("UPDATE jobs SET jd_version=2 WHERE id=?", (job,))

    stale = store.list_approvals(run["id"])["approvals"][0]
    assert stale["is_current"] is False
    assert "changed" in stale["stale_reason"]
    with pytest.raises(Conflict, match="stale"):
        store.resolve_approval("approval-1", True, "User checked old terms.")


def test_unknown_public_listing_fact_needs_exact_quote_then_requeues_evaluation(tmp_path):
    store = configured_store(tmp_path)
    run = store.create_pipeline("acme")
    job = store.discover("acme", listing(arrangement="unknown",
        jd="Full-time systems engineering role in Austin. The workplace is on-site."))["id"]
    store.add_pipeline_job(run["id"], job)
    store.update_pipeline(run["id"], status="running")
    proposal = {"decision": "approve", "reason": "Role appears relevant.", "sponsorship": "offered",
                "required_technologies": [], "preferred_technologies": [], "matched_evidence": [],
                "mandatory_gaps": [], "preferred_gaps": []}

    claim = store.claim("evaluation", "pipeline-evaluator", pipeline_id=run["id"])
    assert store.evaluate(claim, proposal)["decision"] == "ask"
    questions = store.list_pipeline_questions(run["id"])["questions"]
    question = next(item for item in questions if item["kind"] == "listing_fact")
    assert question["is_current"] is True
    assert question["field"] == "arrangement"
    assert question["allowed_values"] == ["onsite", "hybrid", "remote"]
    with pytest.raises(ValueError, match="quote the current"):
        store.answer_listing_fact_question(question["id"], "onsite", "It looks like in-person work.", "Looked at listing")

    saved = store.answer_listing_fact_question(question["id"], "onsite", "The workplace is on-site.",
                                               "Quoted from the public job description.")
    assert saved["status"] == "answered"
    assert store.get_job(job)["run_status"] == "idle"
    claim = store.claim("evaluation", "pipeline-evaluator", pipeline_id=run["id"])
    result = store.evaluate(claim, proposal)
    assert result["decision"] == "approve"
    assert result["effective_listing_facts"]["arrangement"] == "onsite"


def test_stale_listing_question_remains_visible_but_cannot_be_answered(tmp_path):
    store = configured_store(tmp_path)
    run = store.create_pipeline("acme")
    job = store.discover("acme", listing(employment="unknown")) ["id"]
    store.add_pipeline_job(run["id"], job)
    store.update_pipeline(run["id"], status="running")
    proposal = {"decision": "approve", "reason": "Relevant role.", "sponsorship": "offered",
                "required_technologies": [], "preferred_technologies": [], "matched_evidence": [],
                "mandatory_gaps": [], "preferred_gaps": []}
    claim = store.claim("evaluation", "question-evaluator", pipeline_id=run["id"])
    store.evaluate(claim, proposal)
    question = next(item for item in store.list_pipeline_questions(run["id"])["questions"]
                    if item["kind"] == "listing_fact")
    assert question["is_current"] is True

    store.discover("acme", listing(employment="unknown", jd="Updated full-time listing text."))
    stale = next(item for item in store.list_pipeline_questions(run["id"])["questions"]
                 if item["id"] == question["id"])
    assert stale["is_current"] is False
    assert "listing changed" in stale["stale_reason"]
    with pytest.raises(Conflict, match="public listing changed"):
        store.answer_listing_fact_question(question["id"], "full_time", "Updated full-time listing text.",
                                           "Current public listing")


def test_stale_application_question_is_visible_but_write_requires_current_package(tmp_path):
    store = configured_store(tmp_path)
    run = store.create_pipeline("acme")
    job = store.discover("acme", listing())["id"]
    store.add_pipeline_job(run["id"], job)
    store.update_pipeline(run["id"], status="running")
    _add_upload_package(store, job, "application-question-package")
    with store.connect(write=True) as db:
        db.execute("UPDATE jobs SET phase='ready_for_submission' WHERE id=?", (job,))
    claim = store.claim("submission", "question-submitter", pipeline_id=run["id"], job_id=job)
    question = store.request_submission_answer(claim, "Are you legally authorized to work in the US?")
    store.finish_submission(claim, "awaiting_user", "The application needs an exact answer.")
    saved = next(item for item in store.list_pipeline_questions(run["id"])["questions"]
                 if item["id"] == question["id"])
    assert saved["is_current"] is True

    store.learn("New skill", "yes", "Used in a project.", "User correction")
    stale = next(item for item in store.list_pipeline_questions(run["id"])["questions"]
                 if item["id"] == question["id"])
    assert stale["is_current"] is False
    with pytest.raises(Conflict, match="stale"):
        store.answer_submission_question(question["id"], "Yes", "I answered the current application question.")


def test_evaluation_counts_ignore_stale_profile_snapshot_and_submitted_count_survives_jd_change(tmp_path):
    store = configured_store(tmp_path)
    run = store.create_pipeline("acme")
    job_id = store.discover("acme", listing())["id"]
    store.add_pipeline_job(run["id"], job_id)
    store.update_pipeline(run["id"], status="running")
    claim = store.claim("evaluation", "pipeline-evaluator", pipeline_id=run["id"])
    proposal = {"decision": "approve", "reason": "Supported fit.", "sponsorship": "offered",
                "required_technologies": [], "preferred_technologies": [], "matched_evidence": [],
                "mandatory_gaps": [], "preferred_gaps": []}
    store.evaluate(claim, proposal)
    counts = store.get_pipeline(run["id"])["counts"]
    assert (counts["evaluated"], counts["accepted"]) == (1, 1)

    store.learn("New skill", "yes", "Used in a project.", "User correction")
    counts = store.get_pipeline(run["id"])["counts"]
    assert (counts["evaluated"], counts["accepted"]) == (0, 0)

    # A verified historical submission remains terminal even when its listing version changes.
    receipt = tmp_path / "receipt.md"
    receipt.write_text("Your Application Has Been Received. Req R-1", encoding="utf-8")
    store.import_submission(job_id, receipt, "receipt", "fixture")
    store.discover("acme", listing(jd="Updated listing after the application."))
    assert store.get_job(job_id)["phase"] == "submitted"
    assert store.get_pipeline(run["id"])["counts"]["submitted"] == 1


def test_recovery_does_not_restart_runs_and_marks_interrupted_submission_uncertain(tmp_path):
    store = configured_store(tmp_path)
    run = store.create_pipeline("acme")
    job = store.discover("acme", listing())["id"]
    store.add_pipeline_job(run["id"], job)
    store.update_pipeline(run["id"], status="running", discovery_status="complete")
    with store.connect(write=True) as db:
        db.execute("UPDATE jobs SET phase='ready_for_submission',run_status='running',active_stage='submission',lease_token='old' WHERE id=?", (job,))

    store.recover_interrupted_pipelines()

    assert store.get_pipeline(run["id"])["status"] == "blocked"
    assert store.get_job(job)["run_status"] == "submission_uncertain"
    assert store.claim("evaluation", "must-not-auto-start", pipeline_id=run["id"]) is None


def test_scheduler_continues_other_jobs_after_user_checkpoint(tmp_path, monkeypatch):
    store = configured_store(tmp_path)
    run = store.create_pipeline("acme")
    first = store.discover("acme", listing("R-1"))["id"]
    second = store.discover("acme", listing("R-2"))["id"]
    store.add_pipeline_job(run["id"], first)
    store.add_pipeline_job(run["id"], second)
    store.update_pipeline(run["id"], status="running", discovery_status="complete")
    _add_upload_package(store, first, "package-1")
    _add_upload_package(store, second, "package-2")
    with store.connect(write=True) as db:
        db.execute("UPDATE jobs SET phase='ready_for_submission' WHERE id IN (?,?)", (first, second))

    from app import pipelines
    calls = []
    def fake_work_once(store_arg, stage, *, pipeline_id):
        return {"status": "no_work"}
    def fake_submit_once(store_arg, job_id, *, pipeline_id):
        calls.append(job_id)
        claim = store_arg.claim("submission", "fake-submitter", pipeline_id=pipeline_id, job_id=job_id)
        assert claim is not None
        status = "awaiting_user" if job_id == first else "blocked"
        store_arg.finish_submission(claim, status, "Human check required." if status == "awaiting_user" else "Unsupported form step.")
        return {"status": status, "reason": "test fixture"}

    monkeypatch.setattr(pipelines, "work_once", fake_work_once)
    monkeypatch.setitem(__import__("sys").modules, "app.submission", SimpleNamespace(submit_once=fake_submit_once))
    manager = PipelineManager(store)
    try:
        manager._run(run["id"])
    finally:
        manager.shutdown()

    assert set(calls) == {first, second}
    assert store.get_pipeline(run["id"])["status"] == "awaiting_user"
    assert store.get_pipeline(run["id"])["counts"]["submitted"] == 0
    assert store.get_pipeline(run["id"])["counts"]["selected"] == 2


def test_terminal_error_transition_preserves_stop_requested_in_same_write(tmp_path):
    store = configured_store(tmp_path)
    run = store.create_pipeline("acme")
    store.update_pipeline(run["id"], status="running")
    store.request_pipeline_stop(run["id"])

    result = store.finalize_pipeline(run["id"], status="blocked", stage="processing",
                                     blockers=["A task raised an error."], respect_stop=True)

    assert result["status"] == "stopped"
    assert result["stop_requested"] is True
    assert "Stop was requested" in result["blockers"][0]


def test_scheduler_error_racing_with_stop_finishes_as_stopped(tmp_path, monkeypatch):
    store = configured_store(tmp_path)
    run = store.create_pipeline("acme")
    store.update_pipeline(run["id"], status="running", discovery_status="complete")

    def stop_then_fail(_pipeline_id, _excluded):
        store.request_pipeline_stop(run["id"])
        raise RuntimeError("synthetic scheduler error")

    monkeypatch.setattr(store, "pipeline_submission_jobs", stop_then_fail)
    manager = PipelineManager(store)
    try:
        manager._run(run["id"])
    finally:
        manager.shutdown()

    result = store.get_pipeline(run["id"])
    assert result["status"] == "stopped"
    assert result["stop_requested"] is True


def test_global_pause_holds_pipeline_at_checkpoint_until_unpaused(tmp_path):
    store = configured_store(tmp_path)
    run = store.create_pipeline("acme")
    store.update_pipeline(run["id"], discovery_status="complete")
    store.paused(True)
    manager = PipelineManager(store)
    try:
        manager.start(run["id"])
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and store.get_pipeline(run["id"])["status"] != "queued":
            time.sleep(0.01)
        assert store.get_pipeline(run["id"])["status"] == "queued"
        store.paused(False)
        future = manager._futures[run["id"]]
        future.result(timeout=3)
        assert store.get_pipeline(run["id"])["status"] == "completed"
    finally:
        store.paused(False)
        manager.shutdown()


def test_stopping_pipeline_waiting_in_shared_executor_cancels_its_queued_future(tmp_path):
    store = configured_store(tmp_path)
    run = store.create_pipeline("acme")
    executor = ThreadPoolExecutor(max_workers=1)
    occupied, release = Event(), Event()

    def hold_executor():
        occupied.set()
        release.wait(timeout=5)

    blocker = executor.submit(hold_executor)
    assert occupied.wait(timeout=2)
    manager = PipelineManager(store, executor=executor)
    try:
        started = manager.start(run["id"])
        assert started["status"] == "queued"
        future = manager._futures[run["id"]]
        assert not future.done()
        stopped = manager.stop(run["id"])
        assert future.cancelled()
        assert stopped["status"] == "stopped"
        assert stopped["stop_requested"] is True
    finally:
        release.set()
        blocker.result(timeout=2)
        manager.shutdown()


def test_manager_completes_nonempty_pipeline_only_after_real_terminal_transitions(tmp_path, monkeypatch):
    store = configured_store(tmp_path)
    run = store.create_pipeline("acme")
    job_ids = {}
    submissions = []

    def fake_scan(store_arg, company_id, *, pipeline_id):
        approved = store_arg.discover(company_id, listing("R-SUBMIT", title="Systems Engineer"))["id"]
        discarded = store_arg.discover(company_id, listing("R-DISCARD", title="Systems Engineer"))["id"]
        excluded = store_arg.discover(company_id, listing("R-EXCLUDED", title="Support Engineer"))["id"]
        store_arg.add_pipeline_job(pipeline_id, approved)
        store_arg.add_pipeline_job(pipeline_id, discarded)
        store_arg.add_pipeline_job(pipeline_id, excluded, selected=False,
                                   excluded_reason="Title does not match: Systems")
        job_ids.update(approved=approved, discarded=discarded, excluded=excluded)
        return {"status": "complete", "detail": "Fixture feed fully enumerated.", "observed_posts": 3,
                "selected_posts": 2, "excluded_posts": 1}

    def fake_work_once(store_arg, stage, *, pipeline_id):
        if stage == "evaluation":
            claim = store_arg.claim(stage, "fake-evaluator", pipeline_id=pipeline_id)
            if not claim:
                return {"status": "no_work"}
            decision = "discard" if claim["job_id"] == job_ids["discarded"] else "approve"
            result = store_arg.evaluate(claim, {"decision": decision, "reason": "Fixture evaluation.",
                "sponsorship": "offered", "required_technologies": [], "preferred_technologies": [],
                "matched_evidence": [], "mandatory_gaps": [], "preferred_gaps": []})
            return {"status": "completed", "job_id": claim["job_id"], "result": result}
        if stage == "materials":
            claim = store_arg.claim(stage, "fake-materials", pipeline_id=pipeline_id)
            if not claim:
                return {"status": "no_work"}
            package_id = "ready-" + claim["job_id"]
            manifest = {"package_id": package_id, "upload_ready": True, "files": [], "pdf_files": [],
                        "fixture": "offline scheduler contract fixture"}
            with store_arg.connect(write=True) as db:
                db.execute("INSERT INTO packages VALUES(?,?,?,?,?,?)",
                           (package_id, claim["job_id"], claim["jd_version"], claim["profile_version"],
                            json.dumps(manifest), "2026-10-12T00:00:00+00:00"))
                store_arg.release(claim, "ready_for_submission", "idle", "Fixture review passed.", db)
            return {"status": "completed", "job_id": claim["job_id"], "result": manifest}
        return {"status": "no_work"}

    def fake_submit_once(store_arg, job_id, *, pipeline_id):
        claim = store_arg.claim("submission", "fake-submitter", pipeline_id=pipeline_id, job_id=job_id)
        assert claim is not None
        job = store_arg.get_job(job_id)
        receipt = tmp_path / f"{job['requisition_id']}.txt"
        receipt.write_text(f"Your Application Has Been Received. {job['title']} Req {job['requisition_id']}",
                           encoding="utf-8")
        store_arg.import_submission(job_id, receipt, "receipt", "fixture", claim=claim,
            confirmation_text="Your Application Has Been Received", requisition_id=job["requisition_id"],
            job_title=job["title"], source_url=job["url"])
        store_arg.finish_submission(claim, "submitted", "Fixture receipt verified.")
        submissions.append(job_id)
        return {"status": "submitted", "reason": "Fixture receipt verified."}

    from app import pipelines
    monkeypatch.setattr(pipelines, "scan_company", fake_scan)
    monkeypatch.setattr(pipelines, "work_once", fake_work_once)
    monkeypatch.setitem(__import__("sys").modules, "app.submission", SimpleNamespace(submit_once=fake_submit_once))
    manager = PipelineManager(store)
    try:
        manager.start(run["id"])
        manager._futures[run["id"]].result(timeout=5)
        finished = store.get_pipeline(run["id"])
        assert finished["status"] == "completed"
        assert finished["discovery"]["status"] == "complete"
        assert finished["counts"]["discovered"] == 3
        assert finished["counts"]["selected"] == 2
        assert finished["counts"]["excluded"] == 1
        assert finished["counts"]["evaluated"] == 2
        assert finished["counts"]["accepted"] == 1
        assert finished["counts"]["submitted"] == 1
        assert finished["counts"]["discarded"] == 1
        assert finished["counts"]["submitted"] + finished["counts"]["discarded"] == finished["counts"]["selected"]
        assert submissions == [job_ids["approved"]]

        # An independently verified receipt supersedes a stale discard as a terminal state.
        reconciled = store.get_job(job_ids["discarded"])
        external_receipt = tmp_path / "R-DISCARD-external-receipt.txt"
        external_receipt.write_text(
            f"Your Application Has Been Received. {reconciled['title']} Req {reconciled['requisition_id']}",
            encoding="utf-8")
        store.import_submission(job_ids["discarded"], external_receipt, "receipt", "fixture reconciliation")
        after_reconciliation = store.get_pipeline(run["id"])["counts"]
        assert after_reconciliation["submitted"] == 2
        assert after_reconciliation["evaluated"] == 2
        assert after_reconciliation["accepted"] == 1
        assert after_reconciliation["discarded"] == 0
        assert after_reconciliation["submitted"] + after_reconciliation["discarded"] == after_reconciliation["selected"]

        # A later explicit run may observe either historical job, but cannot claim or send them again.
        second = store.create_pipeline("acme")
        store.discover("acme", listing("R-SUBMIT", title="Systems Engineer"))
        store.add_pipeline_job(second["id"], job_ids["approved"])
        store.add_pipeline_job(second["id"], job_ids["discarded"])
        store.update_pipeline(second["id"], discovery_status="complete",
                              discovery={"status": "complete", "detail": "Repeated fixture scan completed."})
        manager.start(second["id"])
        manager._futures[second["id"]].result(timeout=5)
        repeated = store.get_pipeline(second["id"])
        assert repeated["status"] == "completed"
        assert repeated["counts"]["submitted"] == 2
        assert submissions == [job_ids["approved"]]
    finally:
        manager.shutdown()


def test_manager_never_completes_partial_discovery_or_unverified_ready_job(tmp_path, monkeypatch):
    store = configured_store(tmp_path)
    partial = store.create_pipeline("acme")

    from app import pipelines
    monkeypatch.setattr(pipelines, "scan_company", lambda *_args, **_kwargs: {
        "status": "incomplete", "detail": "Feed page cap reached.", "observed_posts": 0})
    manager = PipelineManager(store)
    try:
        manager.start(partial["id"])
        manager._futures[partial["id"]].result(timeout=5)
        assert store.get_pipeline(partial["id"])["status"] == "blocked"
        assert store.get_pipeline(partial["id"])["status"] != "completed"

        ready_run = store.create_pipeline("acme")
        ready_job = store.discover("acme", listing("R-READY"))["id"]
        store.add_pipeline_job(ready_run["id"], ready_job)
        _add_upload_package(store, ready_job, "package-ready-no-receipt")
        with store.connect(write=True) as db:
            db.execute("UPDATE jobs SET phase='ready_for_submission',run_status='idle' WHERE id=?", (ready_job,))
        store.update_pipeline(ready_run["id"], discovery_status="complete",
                              discovery={"status": "complete", "detail": "Fixture scan completed."})
        monkeypatch.setattr(pipelines, "scan_company", lambda *_args, **_kwargs: {
            "status": "complete", "detail": "Fixture scan completed."})
        monkeypatch.setattr(pipelines, "work_once", lambda *_args, **_kwargs: {"status": "no_work"})
        monkeypatch.setitem(__import__("sys").modules, "app.submission", SimpleNamespace(
            submit_once=lambda *_args, **_kwargs: {"status": "submitted", "reason": "No evidence created."}))
        manager.start(ready_run["id"])
        manager._futures[ready_run["id"]].result(timeout=5)
        assert store.get_pipeline(ready_run["id"])["status"] == "blocked"
        assert store.get_job(ready_job)["phase"] == "ready_for_submission"
    finally:
        manager.shutdown()


def test_browser_inventory_gate_requires_sanitized_success_result_not_model_summary():
    from app.server import _browser_inventory_observed
    from app.store import digest

    names, tab_count = ["Chrome"], 0
    normalized = json.dumps({"browser_names": names, "tab_count": tab_count}, sort_keys=True,
                            separators=(",", ":"), ensure_ascii=False)
    report = {"returncode": 0, "timed_out": False,
              "mcp_calls": [{"server": "cua_repl", "tool": "js", "status": "completed"}],
              "unapproved_action_events": [],
              "final": {"status": "connected", "tool_called": "mcp__cua_repl.js", "browser_names": ["Chrome"]},
              "browser_inventory_observation": {"operation": "cua.getState",
                  "source": "successful_cua_repl_js_result", "browser_names": names,
                  "tab_count": tab_count, "result_sha256": digest(normalized)}}
    assert _browser_inventory_observed(report) is True

    model_summary_only = {"mcp_calls": report["mcp_calls"], "final": report["final"]}
    assert _browser_inventory_observed(model_summary_only) is False
    report["browser_inventory_observation"]["result_sha256"] = "0" * 64
    assert _browser_inventory_observed(report) is False

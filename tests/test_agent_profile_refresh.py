from __future__ import annotations

import json

import pytest

from app.store import Conflict, Store


def test_canonical_profile_import_is_idempotent_and_bumps_once(tmp_path):
    store = Store(tmp_path)
    master = tmp_path / "master-resume.md"
    profile = tmp_path / "application-profile.md"
    history = tmp_path / "technology-history.md"
    master.write_text("# Master resume\nVersion one.\n", encoding="utf-8")
    profile.write_text("# Application profile\nVersion one.\n", encoding="utf-8")
    history.write_text("# Technology history\nKeep this source untouched.\n", encoding="utf-8")
    store.import_document("technology_history", history)
    profile_before = store.profile_version()

    first = store.import_canonical_profile_documents(master, profile)
    assert first == {"changed": True, "changed_documents": ["master_resume", "application_profile"],
                     "profile_version": profile_before + 1}
    with store.connect() as db:
        history_before = dict(db.execute("SELECT * FROM source_documents WHERE id='technology_history'").fetchone())
        versions_before = {row["document_id"]: row["count"] for row in db.execute(
            "SELECT document_id,COUNT(*) AS count FROM document_versions GROUP BY document_id")}

    repeated = store.import_canonical_profile_documents(master, profile)
    assert repeated["changed"] is False
    assert repeated["changed_documents"] == []
    assert repeated["profile_version"] == profile_before + 1

    master.write_text("# Master resume\nVersion two.\n", encoding="utf-8")
    changed = store.import_canonical_profile_documents(master, profile)
    assert changed == {"changed": True, "changed_documents": ["master_resume"],
                       "profile_version": profile_before + 2}
    with store.connect() as db:
        history_after = dict(db.execute("SELECT * FROM source_documents WHERE id='technology_history'").fetchone())
        versions_after = {row["document_id"]: row["count"] for row in db.execute(
            "SELECT document_id,COUNT(*) AS count FROM document_versions GROUP BY document_id")}
        assert history_after == history_before
        assert versions_after == {**versions_before, "master_resume": 2}
        assert versions_after["application_profile"] == 1


def _insert_job(db, pipeline_id, company_id, job_id, requisition, now):
    db.execute("INSERT INTO jobs(id,company_id,external_key,requisition_id,url,title,location,country,employment,"
               "arrangement,jd_version,first_seen_at,last_seen_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (job_id, company_id, f"req:{requisition}", requisition,
                f"https://fixture.example/jobs/{requisition}", "Fixture Software Engineer", "Seattle, WA", "US",
                "full_time", "hybrid", 1, now, now))
    db.execute("INSERT INTO job_versions(job_id,version,jd,sha256,metadata,source,observed_at) "
               "VALUES(?,?,?,?,?,?,?)",
               (job_id, 1, "A complete fictional job description.", f"fixture-jd-{job_id}", "{}",
                f"https://fixture.example/jobs/{requisition}", now))
    db.execute("INSERT INTO pipeline_jobs(pipeline_id,job_id,discovered_at,selected,agent_decision,agent_reason,agent_status) "
               "VALUES(?,?,?,1,'approve','Old profile fit','approved')",
               (pipeline_id, job_id, now))


def _seed_refresh_case(tmp_path, *, other_job=False, pending_question=False,
                       possible_send=False, active_worker=False):
    store = Store(tmp_path)
    master = tmp_path / "master-resume.md"
    profile = tmp_path / "application-profile.md"
    master.write_text("# Master resume\nProfile snapshot one.\n", encoding="utf-8")
    profile.write_text("# Profile\nStudent status recorded in historical snapshot.\n", encoding="utf-8")
    store.import_canonical_profile_documents(master, profile)
    pipeline = store.create_agent_pipeline("Fixture Profile Refresh", "https://fixture.example/careers")
    now = "2026-01-01T00:00:00+00:00"
    job_id = "fixture-profile-refresh-job"
    old_profile = store.profile_version()
    with store.connect(write=True) as db:
        _insert_job(db, pipeline["id"], pipeline["company_id"], job_id, "fixture-refresh", now)
        old_evaluator = store._agent_insert_worker(db, pipeline["id"], "evaluator", job_id=job_id,
                                                   context={"historical": "evaluation v1"})
        submission_context = {"document_package_id": "fixture-package-v1",
                              "requested_artifacts": ["resume"], "old_parent_note": "preserve history"}
        if possible_send:
            submission_context["final_action_may_have_been_sent"] = True
        old_parent = store._agent_insert_worker(db, pipeline["id"], "submission", job_id=job_id,
                                                context=submission_context)
        inbox, _ = store._agent_insert_inbox(
            db, db.execute("SELECT * FROM agent_workers WHERE id=?", (old_parent,)).fetchone(),
            "first_application_review", dedupe_key="fixture-review-v1", summary="Review old package",
            action="first_application_review",
            context={"jd_version": 1, "profile_version": old_profile, "ready_artifacts": []})
        db.execute("UPDATE agent_workers SET checkpoint=?,state=? WHERE id=?",
                   ("final_action_in_flight" if possible_send else "", "interrupted" if possible_send else "waiting_approval",
                    old_parent))
        child = store._agent_insert_worker(db, pipeline["id"], "document", job_id=job_id,
                                           parent_id=old_parent,
                                           context={"requested_artifacts": ["resume"], "history": "keep"})
        db.execute("UPDATE agent_workers SET state='completed',result=?,completed_at=?,updated_at=? WHERE id=?",
                   (json.dumps({"disposition": "completed"}), now, now, child))
        question_id = None
        if pending_question:
            pending_worker = store._agent_insert_worker(db, pipeline["id"], "evaluator", job_id=job_id,
                                                        context={"question_history": "pending"})
            pending, _ = store._agent_insert_inbox(
                db, db.execute("SELECT * FROM agent_workers WHERE id=?", (pending_worker,)).fetchone(),
                "question", dedupe_key="fixture-question", question="Please answer this job-local question.",
                context={"jd_version": 1, "profile_version": old_profile})
            question_id = pending["id"]
        if active_worker:
            active_id = store._agent_insert_worker(db, pipeline["id"], "evaluator", job_id=job_id,
                                                   context={"active": True})
            db.execute("UPDATE agent_workers SET state='running',turn_active=1 WHERE id=?", (active_id,))
        db.execute("UPDATE agent_workers SET state='completed',completed_at=?,updated_at=? "
                   "WHERE pipeline_id=? AND role='discovery'", (now, now, pipeline["id"]))
        db.execute("UPDATE pipelines SET discovery_status='complete',status='awaiting_user',updated_at=? WHERE id=?",
                   (now, pipeline["id"]))

    other = None
    master.write_text("# Master resume\nProfile snapshot two.\n", encoding="utf-8")
    profile.write_text("# Profile\nMSc completed in October 2026; no longer a student.\n", encoding="utf-8")
    imported = store.import_canonical_profile_documents(master, profile)
    if other_job:
        with store.connect(write=True) as db:
            other_id = "fixture-unrelated-refresh-job"
            _insert_job(db, pipeline["id"], pipeline["company_id"], other_id, "fixture-unrelated", now)
            other_worker = store._agent_insert_worker(db, pipeline["id"], "submission", job_id=other_id,
                                                      context={"unrelated": "leave unchanged"})
            other_inbox, _ = store._agent_insert_inbox(
                db, db.execute("SELECT * FROM agent_workers WHERE id=?", (other_worker,)).fetchone(),
                "approval", dedupe_key="unrelated-upload", summary="Unrelated current approval", action="upload",
                context={"jd_version": 1, "profile_version": store.profile_version(db), "ready_artifacts": []})
            other = {"job_id": other_id, "worker_id": other_worker, "inbox_id": other_inbox["id"]}
    return {"store": store, "pipeline_id": pipeline["id"], "job_id": job_id,
            "profile_version": imported["profile_version"], "old_profile_version": old_profile,
            "old_evaluator_id": old_evaluator, "old_parent_id": old_parent,
            "inbox_id": inbox["id"], "child_id": child, "question_id": question_id,
            "other": other}


def test_profile_refresh_stales_approval_supersedes_only_target_and_deduplicates(tmp_path):
    case = _seed_refresh_case(tmp_path, other_job=True)
    store = case["store"]
    inbox_before = next(item for item in store.agent_inbox(case["pipeline_id"])
                        if item["id"] == case["inbox_id"])
    assert inbox_before["is_current"] is False
    assert "profile changed" in inbox_before["stale_reason"].lower()
    with pytest.raises(Conflict, match="profile changed"):
        store.decide_agent_inbox(case["inbox_id"], "approve", "A stale review must not be accepted.")
    stale = next(item for item in store.agent_inbox(case["pipeline_id"])
                 if item["id"] == case["inbox_id"])
    assert stale["state"] == "stale"
    assert stale["provenance"].startswith("system:")
    assert stale["response"] == ""

    other_worker_before = store.get_agent_worker(case["other"]["worker_id"])
    other_inbox_before = next(item for item in store.agent_inbox(case["pipeline_id"])
                              if item["id"] == case["other"]["inbox_id"])
    child_before = store.get_agent_worker(case["child_id"])
    result = store.refresh_agent_job_profile(case["pipeline_id"], case["job_id"],
                                             "Applicant completed the MSc in October 2026.")
    assert result["created"] is True
    assert result["already_current"] is False
    assert result["profile_version"] == case["profile_version"]
    assert result["evaluator_worker_id"]
    assert case["inbox_id"] in result["stale_inbox_ids"]
    assert case["old_parent_id"] in result["superseded_worker_ids"]
    assert case["old_evaluator_id"] in result["superseded_worker_ids"]
    assert store.get_agent_worker(case["old_parent_id"])["state"] == "superseded"
    old_context = store.get_agent_worker(case["old_parent_id"])["context"]
    assert old_context["old_parent_note"] == "preserve history"
    assert old_context["profile_refresh_supersession"]["reason"] == "profile_refreshed_before_unsent_work"
    assert store.get_agent_worker(case["child_id"]) == child_before
    assert store.get_agent_worker(case["other"]["worker_id"]) == other_worker_before
    assert next(item for item in store.agent_inbox(case["pipeline_id"])
                if item["id"] == case["other"]["inbox_id"]) == other_inbox_before
    with store.connect() as db:
        membership = db.execute("SELECT agent_decision,agent_reason,agent_status FROM pipeline_jobs "
                                 "WHERE pipeline_id=? AND job_id=?",
                                 (case["pipeline_id"], case["job_id"])).fetchone()
        assert tuple(membership) == ("", "", "selected")
        evaluator = db.execute("SELECT jd_version,profile_version,state FROM agent_workers WHERE id=?",
                               (result["evaluator_worker_id"],)).fetchone()
        assert tuple(evaluator) == (1, case["profile_version"], "queued")

    repeated = store.refresh_agent_job_profile(case["pipeline_id"], case["job_id"],
                                               "Repeat should reuse current evaluator.")
    assert repeated["created"] is False
    assert repeated["already_current"] is True
    assert repeated["evaluator_worker_id"] == result["evaluator_worker_id"]
    assert len([worker for worker in store.list_agent_workers(case["pipeline_id"])
                if worker["job_id"] == case["job_id"] and worker["role"] == "evaluator"
                and worker["profile_version"] == case["profile_version"]]) == 1


@pytest.mark.parametrize(("blocker", "expected"), [
    ("possible_send", "A final application action may have been sent"),
    ("active_worker", "A worker is still active"),
    ("pending_question", "Answer the pending job question"),
])
def test_profile_refresh_refuses_possible_send_active_worker_or_pending_question(tmp_path, blocker, expected):
    case = _seed_refresh_case(tmp_path, possible_send=blocker == "possible_send",
                              active_worker=blocker == "active_worker",
                              pending_question=blocker == "pending_question")
    store = case["store"]
    assessment = store.agent_profile_refresh_eligibility(case["pipeline_id"], case["job_id"])
    assert assessment["eligible"] is False
    assert expected.lower() in assessment["reason"].lower()
    before = store.list_agent_workers(case["pipeline_id"])
    before_inbox = store.agent_inbox(case["pipeline_id"])
    with pytest.raises(Conflict, match=expected):
        store.refresh_agent_job_profile(case["pipeline_id"], case["job_id"], "Must be refused safely.")
    assert store.list_agent_workers(case["pipeline_id"]) == before
    assert store.agent_inbox(case["pipeline_id"]) == before_inbox

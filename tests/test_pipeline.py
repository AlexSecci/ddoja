from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from app.importer import import_existing
from app.server import create_app
from app.store import Conflict, Store, digest
from app.integration import browser_status


def listing(req="123", **changes):
    return {"requisition_id": req, "url": f"https://example.test/jobs/{req}", "title": "C++ engineer",
            "location": "Austin, Texas", "country": "US", "employment": "full_time",
            "arrangement": "onsite", "jd": "Full-time C++ engineering in Austin. Sponsorship offered.", **changes}


def setup_store(tmp_path):
    store = Store(tmp_path)
    store.company("example", "Example", "https://example.test/careers", enabled=True)
    return store


def browser_inventory_report(browser_names):
    normalized = {"browser_names": sorted(set(browser_names)), "tab_count": 0}
    canonical = json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return {"mcp_calls": [{"server": "cua_repl", "tool": "js", "status": "completed"}],
            "unapproved_action_events": [],
            "browser_inventory_observation": {"operation": "cua.getState",
                "source": "successful_cua_repl_js_result", **normalized,
                "result_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest()}}


def proposal(**changes):
    return {"decision": "approve", "reason": "Supported fit.", "sponsorship": "offered",
            "required_technologies": [], "preferred_technologies": [], "matched_evidence": [],
            "mandatory_gaps": [], "preferred_gaps": [], **changes}


def test_dedup_and_changed_description_invalidates_materials(tmp_path):
    store = setup_store(tmp_path)
    created = store.discover("example", listing())
    assert store.discover("example", listing())["created"] is False
    claim = store.claim("evaluation", "first")
    store.evaluate(claim, proposal())
    assert store.get_job(created["id"])["phase"] == "approved"
    changed = store.discover("example", listing(jd="New C++ and Rust requirements."))
    assert changed["id"] == created["id"]
    job = store.get_job(created["id"])
    assert (job["phase"], job["jd_version"]) == ("new", 2)


def test_atomic_claims_allow_only_one_owner(tmp_path):
    store = setup_store(tmp_path)
    store.discover("example", listing())
    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = list(pool.map(lambda n: store.claim("evaluation", str(n)), range(8)))
    assert sum(claim is not None for claim in claims) == 1


def test_shared_unknown_question_and_no_is_remembered(tmp_path):
    store = setup_store(tmp_path)
    for req in ("123", "456"):
        store.discover("example", listing(req))
        store.evaluate(store.claim("evaluation", "worker"), proposal(required_technologies=["FictionalDB"]))
    questions = store.listing("questions")
    assert len(questions) == 1
    assert all(job["run_status"] == "awaiting_information" for job in store.listing("jobs"))
    store.answer_question(questions[0]["id"], "no", "Never used.")
    assert store.technology("FictionalDB")["answer"] == "no"
    assert all(job["run_status"] == "idle" for job in store.listing("jobs"))
    store.evaluate(store.claim("evaluation", "worker"), proposal(decision="discard", reason="Required hands-on knowledge absent.", required_technologies=["FictionalDB"]))
    assert len(store.listing("questions")) == 1


@pytest.mark.parametrize("change", [{"country": "other"}, {"employment": "internship"},
                                    {"employment": "temporary"}, {"arrangement": "remote"}])
def test_hard_filters_override_approval(tmp_path, change):
    store = setup_store(tmp_path)
    store.discover("example", listing(**change))
    result = store.evaluate(store.claim("evaluation", "worker"), proposal())
    assert result["decision"] == "discard"


def test_unknown_sponsorship_is_not_auto_discarded(tmp_path):
    store = setup_store(tmp_path)
    store.discover("example", listing())
    result = store.evaluate(store.claim("evaluation", "worker"), proposal(sponsorship="unknown"))
    assert result["decision"] == "approve"


def test_supported_derived_listing_facts_do_not_overwrite_raw_feed(tmp_path):
    store = setup_store(tmp_path)
    job = store.discover("example", listing(country="unknown", employment="unknown", arrangement="unknown",
        jd="Full-time onsite C++ engineer in Austin, Texas. Sponsorship offered."))
    facts = {"country": "US", "country_evidence": "Austin, Texas", "employment": "full_time",
             "employment_evidence": "Full-time", "arrangement": "onsite", "arrangement_evidence": "onsite"}
    result = store.evaluate(store.claim("evaluation", "worker"), proposal(listing_facts=facts))
    assert result["decision"] == "approve" and result["effective_listing_facts"]["country"] == "US"
    assert store.get_job(job["id"])["country"] == "unknown"


def test_unquoted_listing_fact_cannot_approve_unknown_metadata(tmp_path):
    store = setup_store(tmp_path)
    store.discover("example", listing(arrangement="unknown"))
    with pytest.raises(ValueError, match="exact description/location excerpt"):
        store.evaluate(store.claim("evaluation", "worker"), proposal(listing_facts={"arrangement": "onsite", "arrangement_evidence": "Invented onsite requirement."}))


def test_explicit_sponsorship_exclusion_overrides_approval(tmp_path):
    store = setup_store(tmp_path)
    store.discover("example", listing())
    assert store.evaluate(store.claim("evaluation", "worker"), proposal(sponsorship="excluded"))["decision"] == "discard"


def test_stale_applicant_snapshot_rejects_result(tmp_path):
    store = setup_store(tmp_path)
    store.discover("example", listing())
    claim = store.claim("evaluation", "worker")
    store.learn("CUDA", "yes", "Academic project only.", "test fixture")
    with pytest.raises(Conflict, match="snapshot changed"):
        store.evaluate(claim, proposal())
    store.fail(claim, "Reassess updated applicant facts.")
    assert store.claim("evaluation", "new-worker") is not None


def test_expired_nonbrowser_worker_recovers(tmp_path):
    store = setup_store(tmp_path)
    store.discover("example", listing())
    old = store.claim("evaluation", "old-worker")
    with store.connect(write=True) as db:
        db.execute("UPDATE jobs SET lease_until=0 WHERE id=?", (old["job_id"],))
    new = store.claim("evaluation", "new-worker")
    assert new["job_id"] == old["job_id"] and new["token"] != old["token"]
    with pytest.raises(Conflict):
        store.evaluate(old, proposal())


def test_interrupted_submission_never_requeues(tmp_path):
    store = setup_store(tmp_path)
    job = store.discover("example", listing())
    with store.connect(write=True) as db:
        db.execute("UPDATE jobs SET phase='ready_for_submission',run_status='running',active_stage='submission',lease_until=0 WHERE id=?", (job["id"],))
    assert store.claim("evaluation", "worker") is None
    assert store.get_job(job["id"])["run_status"] == "submission_uncertain"
    store.discover("example", listing(jd="Changed description."))
    assert store.get_job(job["id"])["run_status"] == "submission_uncertain"


def test_jd_change_during_submission_preserves_uncertainty(tmp_path):
    store = setup_store(tmp_path)
    job = store.discover("example", listing())
    with store.connect(write=True) as db:
        db.execute("UPDATE jobs SET phase='ready_for_submission',run_status='running',active_stage='submission' WHERE id=?", (job["id"],))
    store.discover("example", listing(jd="Changed description."))
    assert store.get_job(job["id"])["run_status"] == "submission_uncertain"


def test_browser_lock_and_stale_release(tmp_path):
    store = setup_store(tmp_path)
    token = store.browser_lease("one")
    with pytest.raises(Conflict):
        store.browser_lease("two")
    store.release_browser("wrong-token")
    with pytest.raises(Conflict):
        store.browser_lease("two")
    store.release_browser(token)
    assert store.browser_lease("two") != token


def test_submitted_requires_matching_evidence_and_survives_changes(tmp_path):
    store = setup_store(tmp_path)
    job = store.discover("example", listing())
    evidence = tmp_path / "snapshot.md"
    evidence.write_text("- generic: Other job/456\n", encoding="utf-8")
    with pytest.raises(ValueError):
        store.import_submission(job["id"], evidence, "employer_account", "fixture")
    assert store.get_job(job["id"])["phase"] == "new"
    evidence.write_text("- generic: C++ engineer/123\n", encoding="utf-8")
    store.import_submission(job["id"], evidence, "employer_account", "fixture")
    store.import_submission(job["id"], evidence, "employer_account", "fixture")
    assert len(store.listing("evidence")) == 1
    store.discover("example", listing(jd="Changed after application."))
    assert store.get_job(job["id"])["phase"] == "submitted"
    assert store.claim("evaluation", "worker") is None


def test_materials_validate_sources_and_remain_drafts(tmp_path):
    store = setup_store(tmp_path)
    source = tmp_path / "master.md"
    source.write_text("Built an academic CUDA simulation.", encoding="utf-8")
    store.import_document("master_resume", source)
    store.discover("example", listing())
    store.evaluate(store.claim("evaluation", "worker"), proposal())
    claim = store.claim("materials", "worker")
    package = {"resume_markdown": "Academic CUDA project.", "cover_letter_markdown": "I built a CUDA simulation.",
               "sources": [{"document_id": "master_resume", "excerpt": "Invented achievement."}]}
    with pytest.raises(ValueError, match="exact excerpt"):
        store.store_package(claim, package)
    package["sources"][0]["excerpt"] = "Built an academic CUDA simulation."
    manifest = store.store_package(claim, package)
    assert manifest["upload_ready"] is False
    assert store.listing("jobs")[0]["run_status"] == "awaiting_material_review"


def test_pause_and_submission_gate(tmp_path):
    store = setup_store(tmp_path)
    store.discover("example", listing())
    store.paused(True)
    assert store.claim("evaluation", "worker") is None
    store.paused(False)
    with pytest.raises(Conflict, match="not enabled"):
        store.claim("submission", "worker")


def test_import_four_and_six_column_knowledge_is_idempotent(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "master-resume.md").write_text("Master fixture.", encoding="utf-8")
    (source / "application-profile.md").write_text("US full-time hybrid.", encoding="utf-8")
    (source / "technology-history.md").write_text(
        "| C++ | Yes | CPP | Hands-on project. | User | 2026-10-03 |\n"
        "| Flink | No | Never used. | User, 2026-10-03 |\n", encoding="utf-8")
    sweep = source / "tesla-sweep"
    sweep.mkdir()
    (sweep / "account-check-after-missing-tabs.md").write_text("- generic: 1 Results\n- generic: Engineer/123\n", encoding="utf-8")
    (sweep / "application-tracker.csv").write_text("requisition_id,status,job_url,location\n123,awaiting_captcha_confirmation,https://example.test/123,Austin\n", encoding="utf-8")
    store = Store(tmp_path / "db")
    first = import_existing(store, source)
    version = store.profile_version()
    second = import_existing(store, source)
    assert first["prior_submissions"] == 1
    assert store.technology("CPP")["answer"] == "yes"
    assert store.technology("Flink")["answer"] == "no"
    assert second["documents_changed"] == second["technologies_changed"] == 0
    assert store.profile_version() == version
    assert len(store.listing("evidence")) == 1
    assert store.listing("jobs")[0]["phase"] == "submitted"


def test_local_api_session_and_origin_controls(tmp_path):
    app = create_app(tmp_path)
    with TestClient(app) as client:
        assert client.get("/api/health").json()["release"] == "offline_pipeline"
        assert client.get("/api/summary").status_code == 403
        token = client.get("/api/session").json()["token"]
        headers = {"X-Pipeline-Token": token}
        assert client.get("/api/summary", headers=headers).status_code == 200
        assert client.post("/api/pause", json={"paused": True}, headers={**headers, "Origin": "https://evil.test"}).status_code == 403
        assert client.post("/api/companies", json={"id": "tesla", "name": "Tesla", "careers_url": "https://www.tesla.com/careers", "enabled": True}, headers=headers).status_code == 409
        assert client.post("/api/work/submission", headers=headers).status_code == 422


def test_practice_receipt_accepts_only_fixed_fictional_data(tmp_path):
    app = create_app(tmp_path)
    fixture = (Path(__file__).parent / "fixtures" / "fictional-test-resume.txt").read_text(encoding="utf-8")
    with TestClient(app) as client:
        headers = {"X-Pipeline-Token": client.get("/api/session").json()["token"]}
        payload = {"name": "Test Candidate", "email": "candidate@example.test", "sponsorship": "yes",
                   "filename": "fictional-test-resume.txt", "file_content": fixture}
        response = client.post("/api/practice/receipt", json=payload, headers=headers)
        assert response.status_code == 200
        receipt = response.json()
        assert receipt["test_only"] is True
        assert (tmp_path / "data" / "practice-receipts" / f"{receipt['receipt_id']}.json").exists()
        assert client.post("/api/practice/receipt", json={**payload, "file_content": "Real personal information."}, headers=headers).status_code == 422
        assert client.post("/api/practice/receipt", json={**payload, "email": "real@example.com"}, headers=headers).status_code == 422


def test_browser_visibility_does_not_imply_verified_submission(tmp_path):
    folder = tmp_path / "data" / "integration"
    folder.mkdir(parents=True)
    (folder / "browser.report.json").write_text(json.dumps({"final": {"browser_names": ["Chrome"]}}))
    unverified = browser_status(tmp_path)
    assert unverified["verified"] is False
    assert unverified["inventory_available"] is False
    assert unverified["state"] == "browser_inventory_unverified"
    (folder / "browser.report.json").write_text(json.dumps(browser_inventory_report(["Chrome"])))
    (folder / "practice-complete.report.json").write_text(json.dumps({
        "final": {"status": "failed", "detail": "Blocked"},
        "mcp_calls": [{"failure_detail": "A saved user permission setting blocks this action."}]}))
    status = browser_status(tmp_path)
    assert status["inventory_available"] is True and status["inventory_verified"] is True
    assert status["state"] == "site_permission_required" and status["verified"] is False
    receipt_id = "c" * 32
    final = {"status": "confirmed", "receipt_id": receipt_id, "page_url": "http://127.0.0.1:8765/practice"}
    (folder / "practice-complete.report.json").write_text(json.dumps({"final": final}))
    assert browser_status(tmp_path)["verified"] is False  # Worker prose alone is insufficient evidence.
    receipts = tmp_path / "data" / "practice-receipts"
    receipts.mkdir()
    (receipts / f"{receipt_id}.json").write_text('{"test_only":true}')
    assert browser_status(tmp_path)["verified"] is False
    fixture = (Path(__file__).parent / "fixtures" / "fictional-test-resume.txt").read_text(encoding="utf-8")
    (receipts / f"{receipt_id}.json").write_text(json.dumps({"receipt_id": receipt_id, "test_only": True,
        "name": "Test Candidate", "email": "candidate@example.test", "sponsorship": "yes",
        "filename": "fictional-test-resume.txt", "file_sha256": digest(fixture)}))
    assert browser_status(tmp_path)["verified"] is False  # A local HTTP receipt artifact is not CUA evidence.
    from tools.integration_probe import _practice_receipt_observation
    visible = ("http://127.0.0.1:8765/practice\nLocal practice submission received\n" + receipt_id
               + "\nfictional-test-resume.txt")
    events = [{"type": "item.completed", "item": {"type": "mcp_tool_call", "server": "cua_repl",
               "tool": "js", "status": "completed", "result": {"content": [{"type": "text", "text": visible}]}}}]
    (folder / "practice-complete.report.json").write_text(json.dumps({"final": final,
        "mcp_calls": [{"server": "cua_repl", "tool": "js", "status": "completed"}],
        "unapproved_action_events": [],
        "browser_practice_observation": _practice_receipt_observation(events, final)}))
    assert browser_status(tmp_path)["verified"] is True

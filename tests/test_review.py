import json
from pathlib import Path

import pytest

from app.rendering import render_package
from app.store import Conflict, Store
from app.server import create_app
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]


def draft(tmp_path):
    store = Store(tmp_path)
    store.company("fictional", "Fictional company", "https://example.test", True)
    source = tmp_path / "source.md"
    source.write_text("Test Candidate built a C++ scheduler.")
    store.import_document("master_resume", source)
    job = store.discover("fictional", {"requisition_id": "FIXTURE", "url": "https://example.test/FIXTURE",
        "title": "Software Engineer", "jd": "Build C++ scheduling software.", "country": "US",
        "location": "Austin, Texas", "employment": "full_time", "arrangement": "onsite"})
    store.evaluate(store.claim("evaluation", "fixture"), {"decision": "approve", "reason": "Fictional fixture."})
    manifest = store.store_package(store.claim("materials", "fixture"), {
        "resume_markdown": "# Test Candidate\n\n## Project\n- Built a C++ scheduler.\n",
        "cover_letter_markdown": "Dear team,\n\nI built a C++ scheduler.\n\nTest Candidate\n",
        "sources": [{"document_id": "master_resume", "excerpt": "Test Candidate built a C++ scheduler."}], "answers": []})
    return store, job, manifest


def review(**changes):
    return {"decision": "pass", "issues": [], "layout_issues": [], "claims": [
        {"material_excerpt": "Test Candidate", "verdict": "supported", "document_id": "master_resume", "source_excerpt": "Test Candidate"},
        {"material_excerpt": "Built a C++ scheduler.", "verdict": "supported", "document_id": "master_resume", "source_excerpt": "built a C++ scheduler."},
        {"material_excerpt": "I built a C++ scheduler.", "verdict": "supported", "document_id": "master_resume", "source_excerpt": "built a C++ scheduler."}], **changes}


def test_pdf_roundtrip_and_review_gates_before_readiness(tmp_path):
    store, job, manifest = draft(tmp_path)
    rendered = render_package(store, manifest)
    assert all(item["pages"] == 1 and item["text_matches"] for item in rendered)
    claim = store.claim("review", "fixture")
    invalid = review(claims=[{"material_excerpt": "Test Candidate", "verdict": "supported", "document_id": "master_resume", "source_excerpt": "Invented achievement"}])
    with pytest.raises(ValueError, match="source excerpt"):
        store.finish_review(claim, manifest["package_id"], invalid, rendered)
    with pytest.raises(ValueError, match="both rendered"):
        store.finish_review(claim, manifest["package_id"], review(), [])
    accepted = store.finish_review(claim, manifest["package_id"], review(), rendered)
    assert accepted["upload_ready"] is True and store.get_job(job["id"])["phase"] == "ready_for_submission"
    with pytest.raises(Conflict, match="not enabled"):
        store.claim("submission", "fixture")


def test_pdf_hash_changes_reject_review(tmp_path):
    store, _, manifest = draft(tmp_path)
    rendered = render_package(store, manifest)
    claim = store.claim("review", "fixture")
    Path(rendered[0]["pdf"]).write_bytes(b"Changed after visual inspection")
    with pytest.raises(Conflict, match="evidence changed"):
        store.finish_review(claim, manifest["package_id"], review(), rendered)


def test_failed_factual_review_routes_back_to_new_material_version(tmp_path):
    store, job, manifest = draft(tmp_path)
    rendered = render_package(store, manifest)
    failed = store.finish_review(store.claim("review", "fixture"), manifest["package_id"],
        review(decision="fail", issues=["Unsupported ownership scope."], claims=[
            {"material_excerpt": "Built a C++ scheduler.", "verdict": "needs_clarification", "document_id": "master_resume", "source_excerpt": ""}]), rendered)
    assert failed["upload_ready"] is False
    assert store.get_job(job["id"])["run_status"] == "awaiting_material_correction"
    assert store.claim("materials", "revision") is not None


def test_expired_review_does_not_regenerate_materials(tmp_path):
    store, job, _ = draft(tmp_path)
    store.claim("review", "interrupted")
    with store.connect(write=True) as db:
        db.execute("UPDATE jobs SET lease_until=0 WHERE id=?", (job["id"],))
    assert store.claim("materials", "wrong-stage") is None
    assert store.claim("review", "resumed") is not None


def test_revision_limit_does_not_block_another_job(tmp_path):
    store, job, manifest = draft(tmp_path)
    with store.connect(write=True) as db:
        db.execute("UPDATE jobs SET run_status='awaiting_material_correction',first_seen_at='2000-01-01' WHERE id=?", (job["id"],))
        for n in range(2):
            db.execute("INSERT INTO packages VALUES(?,?,?,?,?,?)", (f"extra{n}", job["id"], 1, store.profile_version(db), json.dumps(manifest), "now"))
    other = store.discover("fictional", {"requisition_id": "OTHER", "url": "https://example.test/OTHER",
        "title": "Software Engineer", "jd": "Build C++ software.", "country": "US",
        "location": "Austin, Texas", "employment": "full_time", "arrangement": "onsite"})
    store.evaluate(store.claim("evaluation", "fixture"), {"decision": "approve", "reason": "Fictional fixture."})
    assert store.claim("materials", "next")["job_id"] == other["id"]
    assert store.get_job(job["id"])["run_status"] == "awaiting_manual_review"


def test_pdf_download_checks_saved_hash_and_local_session(tmp_path):
    store, _, manifest = draft(tmp_path)
    rendered = render_package(store, manifest)
    store.finish_review(store.claim("review", "fixture"), manifest["package_id"], review(), rendered)
    with TestClient(create_app(tmp_path)) as client:
        path = f"/api/packages/{manifest['package_id']}/resume.pdf"
        assert client.get(path).status_code == 403
        headers = {"X-Pipeline-Token": client.get("/api/session").json()["token"]}
        response = client.get(path, headers=headers)
        assert response.status_code == 200 and response.content.startswith(b"%PDF")
        Path(rendered[0]["pdf"]).write_bytes(b"Changed after saved package")
        assert client.get(path, headers=headers).status_code == 409

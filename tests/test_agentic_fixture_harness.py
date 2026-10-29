from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import threading
import time

from fastapi.testclient import TestClient
import pytest

from app.server import create_app
from app.store import Store
from tools.agent_mcp import AgentMCPBridge, ROLE_TOOLS, render_and_register_documents
from tools.agentic_fixture_harness import (
    LISTINGS,
    SYNTHETIC_APPLICANT,
    install_fictional_routes,
    prepare_workspace,
)


class _FixtureRuntime:
    """Synthetic runtime that exercises the real Store, manager, bridge and PDF renderer."""

    def __init__(self, store):
        self.store = store
        self.parent_context = None
        self.ready = threading.Event()
        self.lock = threading.Lock()
        self.submission_turns = {}

    @staticmethod
    def _session(on_event, worker, session_id):
        on_event({"type": "thread.resumed" if worker.get("session_id") else "thread.started",
                  "thread_id": session_id})

    def _bridge(self, task, on_event, session_id):
        worker = task["worker"]
        worker_id = worker["id"]
        capability = task["capability"]

        def action(scoped_worker_id, scoped_capability, name, payload):
            result = self.store.agent_action(scoped_worker_id, scoped_capability, name, payload)
            on_event({"type": "tool_result", "tool_name": "agent_bridge." + name,
                      "success": True, "session_id": session_id})
            return result

        renderer = None
        if worker["role"] == "document":
            renderer = lambda published: render_and_register_documents(
                self.store, worker_id, capability, published)
        return AgentMCPBridge(worker_id, capability, worker["role"], action, renderer)

    def run(self, launch, on_event, should_stop):
        task = launch["task"]
        task["capability"] = launch["capability"]
        worker = task["worker"]
        role, worker_id = worker["role"], worker["id"]
        session_id = worker.get("session_id") or f"fixture-{role}-{worker_id}"
        self._session(on_event, worker, session_id)
        bridge = self._bridge(task, on_event, session_id)

        if role == "discovery":
            for slug, listing in LISTINGS.items():
                selected = slug == "cpp-on-vehicle-data"
                bridge.call("record_listing", {
                    "requisition_id": "fictional-" + slug,
                    "url": f"http://127.0.0.1:8765/__fictional/jobs/{slug}",
                    "title": listing["title"],
                    "jd": listing["jd"],
                    "metadata": {key: listing[key]
                                 for key in ("location", "country", "employment", "arrangement")},
                    "selected": selected,
                    "selection_reason": (
                        "Matches the synthetic title/location test filter."
                        if selected else "Outside the synthetic test's requested title or arrangement."
                    ),
                    "observation": {"test_only": True},
                })
            bridge.call("finish_discovery", {"status": "complete",
                                              "detail": "All three fictional fixture listings were observed.",
                                              "coverage": {"test_only": True, "listing_count": len(LISTINGS)}})
            return {"disposition": "completed", "session_id": session_id}

        if role == "evaluator":
            bridge.call("finish_evaluation", {
                "score": 95, "eligibility": "eligible", "confidence": "high",
                "reason": "The fixed synthetic role fits the synthetic fixture profile.",
                "requirements": [{"requirement": "Fixture engineering", "importance": "core", "match": "met", "evidence": "Synthetic profile"}],
                "hard_blockers": [],
                "observations": {"test_only": True},
            })
            return {"disposition": "completed", "session_id": session_id}

        if role == "submission":
            context = bridge.call("get_context", {"sections": ["prepared_documents"]})["sections"]
            turn = self.submission_turns.get(worker_id, 0) + 1
            self.submission_turns[worker_id] = turn
            if turn == 1:
                bridge.call("request_documents", {
                    "artifacts": ["resume", "cover_letter"],
                    "reason": "The fictional local form requests a resume PDF and cover-letter text.",
                    "form_context": {"test_only": True, "resume": "PDF upload", "cover_letter": "textarea"},
                })
                return {"disposition": "waiting", "session_id": session_id,
                        "checkpoint": "fixture_documents_requested"}

            prepared = context.get("prepared_documents") or []
            resume = next((item for item in prepared if item.get("kind") == "resume"), None)
            cover_letter = next((item for item in prepared if item.get("kind") == "cover_letter"), None)
            if (not resume or resume.get("upload_filename") != "resume.pdf"
                    or not resume.get("upload_path") or not resume.get("upload_sha256")
                    or not cover_letter or "Fictional test candidate" not in cover_letter.get("markdown", "")):
                raise AssertionError("The same submission session did not receive the current prepared documents.")
            with self.lock:
                self.parent_context = context
            self.ready.set()
            bridge.call("request_approval", {
                "action": "first_application_review",
                "summary": "Synthetic fixture review only; no employer action is available.",
                "observation": {
                    "test_only": True,
                    "page_url": "http://127.0.0.1:8765/__fictional/apply/cpp-on-vehicle-data",
                    "page_origin": "http://127.0.0.1:8765",
                    "owned_tab": True,
                    "tab_id": "fictional-owned-tab",
                    "role_evidence": "Fictional C++ on-vehicle data application page, local fixture only.",
                    "form_fields": [
                        {"label": "Name", "value": SYNTHETIC_APPLICANT["name"],
                         "evidence": "Visible name field on the fictional application form."},
                        {"label": "Email", "value": SYNTHETIC_APPLICANT["email"],
                         "evidence": "Visible email field on the fictional application form."},
                    ],
                    "cover_letter_text": cover_letter["markdown"],
                },
            })
            return {"disposition": "waiting", "session_id": session_id,
                    "checkpoint": "fixture_first_application_review"}

        if role == "document":
            bridge.invoke("publish_documents", {"artifacts": [
                {"kind": "resume", "markdown": (
                    "# Test Candidate\n\n"
                    "Fictional systems engineer for the isolated local fixture.\n\n"
                    "## Fictional experience\n"
                    "Built synthetic C++20 Linux services with inter-process messaging and concurrency.\n"
                ), "source_notes": {"test_only": True}},
                {"kind": "cover_letter", "markdown": (
                    "# Fictional test cover letter\n\n"
                    "Fictional test candidate\n\n"
                    "I am interested in the synthetic C++ on-vehicle data role in this local fixture.\n"
                ), "source_notes": {"test_only": True}},
            ]})
            return {"disposition": "completed", "session_id": session_id,
                    "checkpoint": "fixture_documents_published_and_rendered"}

        raise AssertionError(f"Unexpected fixture worker role: {role}")


def _wait_for(predicate, timeout=30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.01)
    raise AssertionError("Timed out waiting for the isolated agent fixture to reach its checkpoint.")


def test_preparing_fixture_creates_only_synthetic_isolated_data(tmp_path):
    workspace = prepare_workspace(tmp_path / "agentic-fixture")
    assert (workspace / "AGENTIC_FIXTURE_TEST_ONLY.json").is_file()
    marker = json.loads((workspace / "AGENTIC_FIXTURE_TEST_ONLY.json").read_text(encoding="utf-8"))
    assert marker["test_only"] is True
    assert marker["final_action_requires_separate_approval"] is True
    assert "@example.invalid" in (workspace / "synthetic-applicant" / "profile.md").read_text(encoding="utf-8")
    store = Store(workspace)
    assert store.listing("jobs") == []
    assert store.listing("companies") == []
    assert len(store.listing("technologies")) == 5
    assert store.listing("source_documents")[0]["id"] == "synthetic_test_profile"


def test_fixture_site_and_receipt_are_test_only_and_require_exact_reviewed_artifact(tmp_path, monkeypatch):
    workspace = prepare_workspace(tmp_path / "agentic-fixture")
    store = Store(workspace)
    import app.rendering as rendering

    original_run = subprocess.run

    def render_only_run(args, *positional, **kwargs):
        # Avoid writing a PDF-authoring marker into the developer's Codex cache;
        # the actual configured PDF renderer and Poppler commands still run.
        if isinstance(args, (list, tuple)) and "--operation-kind" in args:
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        return original_run(args, *positional, **kwargs)

    monkeypatch.setattr(rendering, "CODE_ROOT", workspace)
    monkeypatch.setattr(rendering.subprocess, "run", render_only_run)
    runtime = _FixtureRuntime(store)
    application = create_app(
        workspace,
        runtime_factory=lambda _store: runtime,
        allow_http_loopback_test=True,
    )
    install_fictional_routes(application, workspace)
    client = TestClient(application)
    token = client.get("/api/session").json()["token"]
    headers = {"X-Pipeline-Token": token}
    with client:
        board = client.get("/__fictional/board")
        assert board.status_code == 200
        assert "FICTIONAL LOCAL TEST FIXTURE" in board.text
        assert len(LISTINGS) == 3
        assert "cpp-on-vehicle-data" in board.text
        assert "remote-platform" in board.text
        assert "mechanical-integration" in board.text

        page = client.get("/__fictional/apply/cpp-on-vehicle-data")
        assert page.status_code == 200
        assert 'name="resume" type="file"' in page.text
        assert 'name="cover_letter"' in page.text
        assert "Save fictional test application" in page.text

        started = client.post("/api/pipelines", headers=headers, json={
            "company_name": "Fictional Mobility Systems",
            "careers_url": "http://127.0.0.1:8765/__fictional/board",
            "title_contains": "C++ On-Vehicle Data",
            "location_contains": "Chicago",
        })
        assert started.status_code == 200, started.text
        pipeline = started.json()["pipeline"]
        assert pipeline["agent_test_only"] == 1

        assert runtime.ready.wait(30), "The real agent manager did not publish and render the synthetic documents."
        _wait_for(lambda: any(item["kind"] == "first_application_review" and item["state"] == "pending"
                              for item in store.agent_inbox(pipeline["id"])))
        with runtime.lock:
            parent_context = runtime.parent_context
        assert parent_context is not None
        prepared = parent_context["prepared_documents"]
        resume = next(item for item in prepared if item["kind"] == "resume")
        cover_letter = next(item for item in prepared if item["kind"] == "cover_letter")
        selected_job = next(job for job in store.pipeline_jobs(pipeline["id"])["jobs"]
                            if job["title"] == LISTINGS["cpp-on-vehicle-data"]["title"])
        all_jobs = store.pipeline_jobs(pipeline["id"])["jobs"]
        assert len(all_jobs) == 3
        assert sum(bool(job["selected"]) for job in all_jobs) == 1
        with store.connect() as db:
            artifact_id = db.execute(
                "SELECT id FROM agent_artifacts WHERE pipeline_id=? AND job_id=? AND kind='resume' "
                "AND status='ready' ORDER BY version DESC LIMIT 1",
                (pipeline["id"], selected_job["id"]),
            ).fetchone()["id"]
        artifact_row = store.get_agent_artifact(artifact_id)
        artifact_path = Path(resume["upload_path"])
        pdf = artifact_path.read_bytes()
        checksum = hashlib.sha256(pdf).hexdigest()
        assert artifact_row["status"] == "ready"
        assert artifact_row["pdf_sha256"] == checksum == resume["upload_sha256"]
        assert artifact_path.name == "resume.pdf" and artifact_path.parent.name == "rendered"
        assert "Fictional test candidate" in cover_letter["markdown"]

        # The first-application review must expose the exact current package and
        # what the submission agent actually observed, without turning the
        # agent's observation into an approval.
        inbox_response = client.get(f"/api/pipelines/{pipeline['id']}/inbox", headers=headers)
        assert inbox_response.status_code == 200
        review = next(item for item in inbox_response.json()["items"]
                      if item["kind"] == "first_application_review")
        review_context = review["context"]
        assert review["state"] == "pending"
        assert review_context["owned_tab"] is True
        assert review_context["tab_id"] == "fictional-owned-tab"
        assert review_context["page_url"].endswith("/__fictional/apply/cpp-on-vehicle-data")
        assert review_context["page_origin"] == "http://127.0.0.1:8765"
        assert review_context["role_evidence"].startswith("Fictional C++")
        assert review_context["form_fields"] == [
            {"label": "Name", "value": SYNTHETIC_APPLICANT["name"],
             "evidence": "Visible name field on the fictional application form."},
            {"label": "Email", "value": SYNTHETIC_APPLICANT["email"],
             "evidence": "Visible email field on the fictional application form."},
        ]
        assert review_context["cover_letter_text"] == cover_letter["markdown"]
        reviewed_docs = {item["kind"]: item for item in review_context["review_artifacts"]}
        assert set(reviewed_docs) == {"resume", "cover_letter"}
        assert reviewed_docs["cover_letter"]["markdown"] == cover_letter["markdown"]
        assert reviewed_docs["resume"]["upload"]["filename"] == "resume.pdf"
        assert reviewed_docs["resume"]["upload"]["sha256"] == checksum

        # Download the same hash-checked variants linked from the review card.
        for kind, document in reviewed_docs.items():
            pdf_response = client.get(
                f"/api/agent-artifacts/{document['id']}?variant=pdf", headers=headers)
            assert pdf_response.status_code == 200
            assert pdf_response.headers["content-type"].startswith("application/pdf")
            assert hashlib.sha256(pdf_response.content).hexdigest() == document["upload"]["sha256"]
            markdown_response = client.get(
                f"/api/agent-artifacts/{document['id']}?variant=markdown", headers=headers)
            assert markdown_response.status_code == 200
            assert markdown_response.content.decode("utf-8") == document["markdown"]
            for preview in document["previews"]:
                preview_response = client.get(
                    f"/api/agent-artifacts/{document['id']}?variant=preview&index={preview['index']}",
                    headers=headers)
                assert preview_response.status_code == 200
                assert hashlib.sha256(preview_response.content).hexdigest() == preview["sha256"]

        # Role-scoped agents can request an approval but cannot resolve it.
        assert "decision" not in ROLE_TOOLS["submission"]
        bridge = AgentMCPBridge("synthetic-submission-worker", "synthetic-capability", "submission",
                                lambda *_args: pytest.fail("The agent must not reach the approval decision handler."))
        with pytest.raises(ValueError, match="not available"):
            bridge.call("decide_agent_inbox", {"decision": "approve"})

        form_fields = {**SYNTHETIC_APPLICANT, "cover_letter": cover_letter["markdown"]}
        wrong_candidate = dict(form_fields, name="Real Person")
        rejected = client.post("/__fictional/receipt/cpp-on-vehicle-data", data=wrong_candidate,
                               files={"resume": ("resume.pdf", pdf, "application/pdf")})
        assert rejected.status_code == 422
        assert list((workspace / "data" / "fictional-test-receipts").glob("*.json")) == []

        receipt_response = client.post("/__fictional/receipt/cpp-on-vehicle-data", data=form_fields,
                                       files={"resume": ("resume.pdf", pdf, "application/pdf")})
        assert receipt_response.status_code == 200, receipt_response.text
        receipt_files = list((workspace / "data" / "fictional-test-receipts").glob("*.json"))
        assert len(receipt_files) == 1
        receipt = json.loads(receipt_files[0].read_text(encoding="utf-8"))
        assert receipt["test_only"] is True
        assert receipt["applicant"] == SYNTHETIC_APPLICANT
        assert receipt["resume_sha256"] == checksum
        assert receipt["reviewed_artifact_id"] == artifact_id
        assert receipt["resume_source_sha256"] == artifact_row["sha256"]
        assert receipt["resume_package"] == resume["package_id"]
        assert receipt["cover_letter_present"] is True
        assert "cover_letter" not in receipt

        artifact_path.write_bytes(b"%PDF-1.7\nchanged after review\n%%EOF\n")
        stale_download = client.get(
            f"/api/agent-artifacts/{reviewed_docs['resume']['id']}?variant=pdf", headers=headers)
        assert stale_download.status_code == 409
        mismatch = client.post("/__fictional/receipt/cpp-on-vehicle-data", data=form_fields,
                               files={"resume": ("resume.pdf", pdf, "application/pdf")})
        assert mismatch.status_code == 409
        assert len(list((workspace / "data" / "fictional-test-receipts").glob("*.json"))) == 1

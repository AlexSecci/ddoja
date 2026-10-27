from __future__ import annotations

import threading
import time
import hashlib
import json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.pipelines import AgentPipelineManager
from app.store import Conflict, Store


class StreamingFixtureRuntime:
    """Local runtime harness; no model, browser, or network requests are made."""

    def __init__(self, store):
        self.store = store
        self.evaluator_started = threading.Event()
        self.submission_started = threading.Event()
        self._lock = threading.Lock()
        self._turns_by_worker = {}
        self.order = []

    def _record(self, value):
        with self._lock:
            self.order.append(value)

    def _action(self, task, name, payload):
        return self.store.agent_action(task["worker"]["id"], task["capability"], name, payload)

    @staticmethod
    def _emit(on_event, tool_name, session_id):
        on_event({"type": "tool_result", "tool_name": tool_name,
                  "success": True, "session_id": session_id})

    def run(self, launch, on_event, should_stop):
        task = launch["task"]
        task["capability"] = launch["capability"]
        worker = task["worker"]
        worker_id, role = worker["id"], worker["role"]

        if role == "discovery":
            session_id = "fixture-discovery-session"
            self._record("discovery_started")
            on_event({"type": "thread.started", "thread_id": session_id})
            self._action(task, "record_listing", {
                "requisition_id": "fixture-req-1",
                "url": "https://fixture.example/jobs/fixture-req-1",
                "title": "Fixture Software Engineer",
                "jd": "A complete fictional listing used only by the isolated manager test.",
                "metadata": {"location": "Seattle, WA", "country": "US",
                             "employment": "full_time", "arrangement": "hybrid"},
                "selected": True,
                "selection_reason": "Selected by the fixture discovery agent.",
                "observation": {"source": "test fixture"},
            })
            self._emit(on_event, "agent_bridge.record_listing", session_id)
            assert self.evaluator_started.wait(5), "listing did not dispatch an evaluator during discovery"
            assert self.submission_started.wait(5), "evaluation did not dispatch submission during discovery"
            self._record("discovery_finishing")
            self._action(task, "finish_discovery", {
                "status": "complete", "detail": "The fixture discovery pass reached its complete boundary.",
                "coverage": {"fixture_listings": 1},
            })
            self._emit(on_event, "agent_bridge.finish_discovery", session_id)
            return {"disposition": "completed", "session_id": session_id, "checkpoint": "discovery_done"}

        session_id = worker.get("session_id") or f"fixture-{role}-session"
        event_type = "thread.resumed" if worker.get("session_id") else "thread.started"
        on_event({"type": event_type, "thread_id": session_id})

        if role == "evaluator":
            self._record("evaluator_started")
            self.evaluator_started.set()
            self._action(task, "finish_evaluation", {
                "decision": "approve", "reason": "Fixture evaluator approved the scoped listing.",
                "observations": {"fixture": True},
            })
            self._emit(on_event, "agent_bridge.finish_evaluation", session_id)
            assert self.submission_started.wait(5), "approval did not dispatch submission before evaluation ended"
            return {"disposition": "completed", "session_id": session_id, "checkpoint": "evaluation_done"}

        if role == "submission":
            with self._lock:
                turn = self._turns_by_worker.get(worker_id, 0) + 1
                self._turns_by_worker[worker_id] = turn
            self._record("submission_started")
            self.submission_started.set()
            if turn == 1:
                result = self._action(task, "request_approval", {
                    "action": "first_application_review",
                    "summary": "Review the exact fictional application before its final action.",
                    "observation": {"fixture": True},
                })
                self._emit(on_event, "agent_bridge.request_approval", session_id)
                return {"disposition": "waiting", "session_id": session_id,
                        "checkpoint": f"inbox:{result['inbox_item_id']}"}

            authorization = self._action(task, "begin_final_action", {})
            assert authorization["authorized"] is True
            self._emit(on_event, "cua_repl.js", session_id)
            result = self._action(task, "finish_submission", {
                "status": "submitted", "detail": "Fixture submission has a successful current-session CUA result.",
                "observations": {"fixture": True},
            })
            assert result["status"] == "submitted"
            self._emit(on_event, "agent_bridge.finish_submission", session_id)
            # A committed Store result must not make the stop callback interrupt
            # its own same-session acknowledgment/cleanup phase.
            assert not should_stop()
            assert self.store.get_pipeline(worker["pipeline_id"])["status"] != "completed"
            self._record("submission_cleanup_completed")
            return {"disposition": "completed", "session_id": session_id,
                    "checkpoint": "submission_cleanup_complete"}

        raise AssertionError(f"Unexpected fixture worker role: {role}")


def _wait_for(predicate, *, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("Timed out waiting for the fixture manager state transition.")


@pytest.mark.parametrize(("existing_status", "expected_status"), [
    (None, "pending"),
    ("unknown", "unknown"),
    ("accepted", "accepted"),
    ("rejected", "rejected"),
])
def test_streaming_listing_dispatches_and_preserves_recruiter_outcome(
        tmp_path, existing_status, expected_status):
    store = Store(tmp_path)
    pipeline = store.create_agent_pipeline("Fixture Software", "https://fixture.example/careers")
    runtime = StreamingFixtureRuntime(store)
    manager = AgentPipelineManager(store, runtime_factory=lambda _store: runtime)
    try:
        manager.start(pipeline["id"])

        pending_review = _wait_for(lambda: next((item for item in store.agent_inbox(pipeline["id"])
                                                 if item["kind"] == "first_application_review"
                                                 and item["state"] == "pending"), None))
        job_id = pending_review["job_id"]
        existing_provenance = "Imported synthetic recruiter outcome; keep it unchanged."
        if existing_status:
            # Model an already-recorded import/manual outcome. The successful
            # agent submission must create a pending row only if none exists.
            with store.connect(write=True) as db:
                db.execute("INSERT INTO application_outcomes(job_id,status,provenance,actor,updated_at) "
                           "VALUES(?,?,?,?,?)",
                           (job_id, existing_status, existing_provenance, "importer", "fixture-time"))
        store.decide_agent_inbox(pending_review["id"], "approve", "Explicit fixture test review.")
        manager.schedule()

        def completed_pipeline():
            candidate = store.get_pipeline(pipeline["id"])
            return candidate if candidate["status"] == "completed" else None

        final_pipeline = _wait_for(completed_pipeline)
        counts = final_pipeline["counts"]
        assert final_pipeline["discovery_status"] == "complete"
        assert (counts["discovered"], counts["selected"], counts["evaluated"],
                counts["accepted"], counts["submitted"]) == (1, 1, 1, 1, 1)
        recruiter_counts = {key: value for key, value in counts.items() if key.startswith("recruiter_")}
        assert recruiter_counts["recruiter_" + expected_status] == 1
        assert sum(recruiter_counts.values()) == 1
        outcome = store.get_job(job_id)["application_outcome"]
        assert outcome["status"] == expected_status
        if existing_status:
            assert outcome["provenance"] == existing_provenance
            assert outcome["actor"] == "importer"
        else:
            assert outcome["actor"] == "agent_submission"
            assert "awaiting recruiter response" in outcome["provenance"]
            assert "polling is not configured" in outcome["provenance"]
        assert runtime.order.index("evaluator_started") < runtime.order.index("discovery_finishing")
        assert runtime.order.index("submission_started") < runtime.order.index("discovery_finishing")
        assert runtime.order.index("submission_cleanup_completed") > runtime.order.index("discovery_finishing")
        assert store.list_agent_workers(pipeline["id"])
        assert all(worker["state"] in {"completed", "discarded"}
                   for worker in store.list_agent_workers(pipeline["id"]))
    finally:
        manager.shutdown()


def test_loopback_test_pipeline_preserves_fixture_http_origin(tmp_path):
    store = Store(tmp_path)
    pipeline = store.create_agent_pipeline(
        "Fictional Local Board", "http://127.0.0.1:49152/careers", allow_http_loopback_test=True, test_only=True)
    worker = store.list_agent_workers(pipeline["id"])[0]
    store.reserve_agent_worker(worker["id"])
    launch = store.agent_launch_context(worker["id"])
    assert launch["task"]["task_context"]["pipeline"]["careers_url"] == "http://127.0.0.1:49152/careers"
    assert launch["task"]["task_context"]["pipeline"]["test_only"] is True


def _create_document_parent(store):
    pipeline = store.create_agent_pipeline("Fixture Document Co", "https://fixture.example/careers")
    discovery = store.list_agent_workers(pipeline["id"])[0]
    assert store.reserve_agent_worker(discovery["id"])
    discovery_launch = store.agent_launch_context(discovery["id"])
    discovery_task = discovery_launch["task"]
    listing = store.agent_action(discovery["id"], discovery_launch["capability"], "record_listing", {
        "requisition_id": "fixture-document-1", "url": "https://fixture.example/jobs/fixture-document-1",
        "title": "Fixture Application Engineer", "jd": "Fictional job description for the document test.",
        "metadata": {"location": "Seattle, WA", "country": "US", "employment": "full_time",
                     "arrangement": "hybrid"},
        "selected": True, "selection_reason": "Selected in an isolated test.",
    })
    evaluator = next(worker for worker in store.list_agent_workers(pipeline["id"])
                     if worker["role"] == "evaluator")
    assert store.reserve_agent_worker(evaluator["id"])
    evaluation_launch = store.agent_launch_context(evaluator["id"])
    store.agent_action(evaluator["id"], evaluation_launch["capability"], "finish_evaluation", {
        "decision": "approve", "reason": "Fixture job accepted for the document workflow."})
    parent = next(worker for worker in store.list_agent_workers(pipeline["id"])
                  if worker["role"] == "submission")
    assert store.reserve_agent_worker(parent["id"])
    parent_launch = store.agent_launch_context(parent["id"])
    child_info = store.agent_action(parent["id"], parent_launch["capability"], "request_documents", {
        "artifacts": ["resume"], "reason": "The fixture form presents a resume field.",
        "form_context": {"fixture": True},
    })
    child = store.get_agent_worker(child_info["child_worker_id"])
    assert listing["job_id"] == parent["job_id"] == child["job_id"]
    return pipeline, parent, child


def test_document_render_registration_binds_current_pdf_and_prepared_markdown(tmp_path):
    store = Store(tmp_path)
    pipeline, parent, child = _create_document_parent(store)
    assert store.reserve_agent_worker(child["id"])
    child_launch = store.agent_launch_context(child["id"])
    markdown = "# Fixture Resume\n\nAuthored only for the isolated renderer test.\n"
    published = store.agent_action(child["id"], child_launch["capability"], "publish_documents", {
        "artifacts": [{"kind": "resume", "markdown": markdown,
                       "source_notes": {"fixture": True}}],
    })
    source = published["artifacts"][0]
    package_dir = Path(tmp_path) / "artifacts" / "agent" / published["package_id"]
    pdf = b"%PDF-1.7\nfixture-rendered-pdf\n%%EOF\n"
    preview = b"fixture-preview-image"
    pdf_path = package_dir / "rendered" / "resume.pdf"
    preview_path = package_dir / "rendered" / "page-1.png"
    pdf_path.parent.mkdir(parents=True)
    pdf_path.write_bytes(pdf)
    preview_path.write_bytes(preview)
    pdf_relative = pdf_path.relative_to(tmp_path).as_posix()
    preview_relative = preview_path.relative_to(tmp_path).as_posix()
    registered = store.agent_action(child["id"], child_launch["capability"], "record_document_renders", {
        "artifacts": [{"artifact_id": source["id"], "source_sha256": source["sha256"],
                       "pdf_path": pdf_relative, "pdf_sha256": hashlib.sha256(pdf).hexdigest(),
                       "previews": [{"path": preview_relative, "sha256": hashlib.sha256(preview).hexdigest()}]}],
    })
    assert registered["recorded"][0]["pdf_sha256"] == hashlib.sha256(pdf).hexdigest()
    result = store.finish_agent_turn(child["id"], {"disposition": "completed"})
    assert result["state"] == "completed"
    assert store.get_agent_artifact(source["id"])["status"] == "ready"

    assert store.reserve_agent_worker(parent["id"])
    parent_launch = store.agent_launch_context(parent["id"])
    parent_context = store.agent_action(parent["id"], parent_launch["capability"], "get_context",
                                        {"sections": ["prepared_documents"]})["sections"]
    prepared = parent_context["prepared_documents"]
    assert len(prepared) == 1
    assert prepared[0]["markdown"] == markdown
    assert prepared[0]["upload_path"] == str(pdf_path.resolve())
    assert prepared[0]["upload_sha256"] == hashlib.sha256(pdf).hexdigest()
    assert prepared[0]["preview_paths"] == [{"path": preview_relative,
                                              "sha256": hashlib.sha256(preview).hexdigest()}]

    review_observation = {"destination": "http://fixture.example/application",
                          "tab_id": "fixture-owned-tab",
                          "fields": [{"name": "resume", "value": "current rendered resume PDF"}],
                          "observations": {"fixture_only": True}}
    review_result = store.agent_action(parent["id"], parent_launch["capability"], "request_approval", {
        "action": "first_application_review", "summary": "Review this isolated test application.",
        "observation": review_observation,
    })
    review = next(item for item in store.agent_inbox(pipeline["id"])
                  if item["id"] == review_result["inbox_item_id"])
    context = review["context"]
    # Keep the binding snapshot structurally compact; the review presentation is
    # deliberately separate and includes the authored text plus verified files.
    assert context["ready_artifacts"] == [{
        "kind": "resume", "filename": "resume.md", "sha256": source["sha256"], "version": 1,
        "package_id": published["package_id"], "pdf_sha256": hashlib.sha256(pdf).hexdigest(),
    }]
    assert context["review_artifacts"][0]["id"] == source["id"]
    assert context["review_artifacts"][0]["markdown"] == markdown
    assert context["review_artifacts"][0]["source_url"] == \
        f"/api/agent-artifacts/{source['id']}?variant=markdown"
    upload = context["review_artifacts"][0]["upload"]
    assert upload == {"filename": "resume.pdf",
                      "path": f"artifacts/agent/{published['package_id']}/rendered/resume.pdf",
                      "sha256": hashlib.sha256(pdf).hexdigest(),
                      "url": f"/api/agent-artifacts/{source['id']}?variant=pdf"}
    assert context["review_artifacts"][0]["previews"] == [{
        "index": 0, "path": f"artifacts/agent/{published['package_id']}/rendered/page-1.png",
        "filename": "page-1.png", "sha256": hashlib.sha256(preview).hexdigest(),
        "url": f"/api/agent-artifacts/{source['id']}?variant=preview&index=0",
    }]
    assert context["destination"] == review_observation["destination"]
    assert context["fields"] == review_observation["fields"]
    assert "capability" not in context

    markdown_variant = store.get_agent_artifact_variant(source["id"], "markdown")
    pdf_variant = store.get_agent_artifact_variant(source["id"], "pdf")
    preview_variant = store.get_agent_artifact_variant(source["id"], "preview", index=0)
    assert Path(markdown_variant["path"]).read_text(encoding="utf-8") == markdown
    assert pdf_variant["sha256"] == upload["sha256"]
    assert preview_variant["sha256"] == hashlib.sha256(preview).hexdigest()

    store.decide_agent_inbox(review["id"], "approve", "Reviewed only the isolated fictional application.")
    assert store.reserve_agent_worker(parent["id"])
    resumed = store.agent_launch_context(parent["id"])
    authorization = store.agent_action(parent["id"], resumed["capability"], "begin_final_action", {})
    assert authorization["authorized"] is True

    pdf_path.write_bytes(pdf + b"tampered")
    with pytest.raises(Conflict, match="does not match|no longer matches"):
        store.get_agent_artifact_variant(source["id"], "pdf")


def test_worker_checkpoint_cannot_forge_pre_send_or_document_package_state(tmp_path):
    store = Store(tmp_path)
    pipeline = store.create_agent_pipeline("Fixture Protected Context", "https://fixture.example/careers")
    worker = store.list_agent_workers(pipeline["id"])[0]
    assert store.reserve_agent_worker(worker["id"])
    launch = store.agent_launch_context(worker["id"])
    for forged in ({"final_action_may_have_been_sent": True},
                   {"document_package_id": "outside-model-control"},
                   {"ready_artifacts": [{"path": "invented.pdf"}]},
                   {"snapshot_supersession": {"replacement_worker_id": "forged"}}):
        try:
            store.agent_action(worker["id"], launch["capability"], "checkpoint",
                               {"checkpoint": "ordinary", "context": forged})
        except Conflict:
            pass
        else:
            raise AssertionError(f"host-owned context key was accepted: {forged}")

    pipeline, parent, child = _create_document_parent(store)
    assert store.reserve_agent_worker(child["id"])
    child_launch = store.agent_launch_context(child["id"])
    published = store.agent_action(child["id"], child_launch["capability"], "publish_documents", {
        "artifacts": [{"kind": "resume", "markdown": "# Fixture\n", "source_notes": {}}],
    })
    pdf = b"%PDF-1.7\nnot-in-this-package\n%%EOF\n"
    outside = Path(tmp_path) / "artifacts" / "agent" / "different-package" / "resume.pdf"
    outside.parent.mkdir(parents=True)
    outside.write_bytes(pdf)
    try:
        store.agent_action(child["id"], child_launch["capability"], "record_document_renders", {
            "artifacts": [{"artifact_id": published["artifacts"][0]["id"],
                           "source_sha256": published["artifacts"][0]["sha256"],
                           "pdf_path": outside.relative_to(tmp_path).as_posix(),
                           "pdf_sha256": hashlib.sha256(pdf).hexdigest(), "previews": []}],
        })
    except Conflict:
        pass
    else:
        raise AssertionError("A PDF outside the host-generated package was accepted.")


def _seed_resume_snapshot_case(tmp_path, *, mismatch="jd", replacement=True, possible_send=False):
    """Create durable, isolated workers representing a snapshot change during discovery."""
    store = Store(tmp_path)
    pipeline = store.create_agent_pipeline("Fixture Snapshot Resume", "https://fixture.example/careers")
    now = "2026-01-01T00:00:00+00:00"
    with store.connect(write=True) as db:
        company_id = pipeline["company_id"]
        current_profile = store.profile_version(db)
        if mismatch == "profile":
            old_profile = current_profile
            current_profile += 1
            db.execute("UPDATE meta SET value=? WHERE key='profile_version'", (str(current_profile),))
        else:
            old_profile = current_profile
        old_jd = 1
        current_jd = 2 if mismatch == "jd" else 1
        job_id = "fixture-snapshot-job"
        db.execute("INSERT INTO jobs(id,company_id,external_key,requisition_id,url,title,location,country,employment,"
                   "arrangement,jd_version,first_seen_at,last_seen_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (job_id, company_id, "req:fixture-snapshot", "fixture-snapshot",
                    "https://fixture.example/jobs/fixture-snapshot", "Fixture Systems Engineer",
                    "Seattle, WA", "US", "full_time", "hybrid", current_jd, now, now))
        for version in range(1, current_jd + 1):
            description = f"Fictional complete job description version {version}."
            db.execute("INSERT INTO job_versions(job_id,version,jd,sha256,metadata,source,observed_at) "
                       "VALUES(?,?,?,?,?,?,?)",
                       (job_id, version, description, f"fixture-sha-{version}",
                        json.dumps({"location": "Seattle, WA", "country": "US", "employment": "full_time",
                                    "arrangement": "hybrid"}),
                        "https://fixture.example/jobs/fixture-snapshot", now))
        db.execute("INSERT INTO pipeline_jobs(pipeline_id,job_id,discovered_at,selected,agent_status) "
                   "VALUES(?,?,?,1,'selected')", (pipeline["id"], job_id, now))

        old_worker_id = store._agent_insert_worker(db, pipeline["id"], "submission", job_id=job_id,
                                                   context={"checkpoint_notes": {"preserve": "old history"}})
        db.execute("UPDATE agent_workers SET jd_version=?,profile_version=?,state='interrupted',session_id=?,"
                   "resume_mode='same_session',checkpoint='reconciliation_required',context=?,result=?,error=?,"
                   "completed_at=?,updated_at=? WHERE id=?",
                   (old_jd, old_profile, "fixture-old-session",
                    json.dumps({"checkpoint_notes": {"preserve": "old history"},
                                **({"final_action_may_have_been_sent": True} if possible_send else {})}),
                    json.dumps({"disposition": "waiting", "checkpoint": "reconciliation_required"}),
                    "Saved old-snapshot checkpoint.", now, now, old_worker_id))
        db.execute("INSERT INTO agent_runtime_events(id,worker_id,session_id,event_type,tool_name,success,payload,created_at) "
                   "VALUES(?,?,?,?,?,?,?,?)",
                   ("fixture-old-runtime-event", old_worker_id, "fixture-old-session", "tool_result",
                    "agent_bridge.checkpoint", 1, json.dumps({"checkpoint": "reconciliation_required"}), now))

        current_worker_id = None
        child_worker_id = None
        inbox_id = None
        if replacement:
            current_worker_id = store._agent_insert_worker(db, pipeline["id"], "submission", job_id=job_id,
                                                            context={"current_snapshot_note": "preserve"})
            current_row = db.execute("SELECT * FROM agent_workers WHERE id=?", (current_worker_id,)).fetchone()
            inbox, _created = store._agent_insert_inbox(
                db, current_row, "approval", dedupe_key="fixture-current-upload",
                summary="Review the current-snapshot upload", action="upload",
                context={"jd_version": current_jd, "profile_version": current_profile,
                         "ready_artifacts": [{"kind": "resume", "sha256": "fixture-current-pdf"}]})
            inbox_id = inbox["id"]
            child_worker_id = store._agent_insert_worker(db, pipeline["id"], "document", job_id=job_id,
                                                          parent_id=current_worker_id,
                                                          context={"document_package_id": "fixture-package-v2"})
            db.execute("UPDATE agent_workers SET state='completed',result=?,completed_at=?,updated_at=? WHERE id=?",
                       (json.dumps({"disposition": "completed"}), now, now, child_worker_id))

        discovery = db.execute("SELECT id FROM agent_workers WHERE pipeline_id=? AND role='discovery'",
                               (pipeline["id"],)).fetchone()
        db.execute("UPDATE agent_workers SET state='completed',completed_at=?,updated_at=? WHERE id=?",
                   (now, now, discovery["id"]))
        db.execute("UPDATE pipelines SET discovery_status='complete',status='interrupted',stage='interrupted',"
                   "stop_requested=0,updated_at=? WHERE id=?", (now, pipeline["id"]))
    return {"store": store, "pipeline_id": pipeline["id"], "job_id": job_id,
            "old_worker_id": old_worker_id, "current_worker_id": current_worker_id,
            "child_worker_id": child_worker_id, "inbox_id": inbox_id,
            "old_jd": old_jd, "old_profile": old_profile,
            "current_jd": current_jd, "current_profile": current_profile}


@pytest.mark.parametrize("mismatch", ["jd", "profile"])
def test_explicit_resume_supersedes_only_stale_unsent_worker_and_preserves_current_work(tmp_path, mismatch):
    case = _seed_resume_snapshot_case(tmp_path, mismatch=mismatch)
    store = case["store"]
    current_before = store.get_agent_worker(case["current_worker_id"])
    child_before = store.get_agent_worker(case["child_worker_id"])
    inbox_before = next(item for item in store.agent_inbox(case["pipeline_id"])
                        if item["id"] == case["inbox_id"])
    old_events_before = None
    with store.connect() as db:
        old_events_before = [dict(row) for row in db.execute(
            "SELECT id,worker_id,session_id,event_type,tool_name,success,payload,created_at "
            "FROM agent_runtime_events WHERE worker_id=? ORDER BY id", (case["old_worker_id"],))]

    resumed = store.resume_agent_pipeline(case["pipeline_id"])
    old_after = store.get_agent_worker(case["old_worker_id"])
    assert old_after["state"] == "superseded"
    assert old_after["session_id"] == "fixture-old-session"
    assert old_after["checkpoint"] == "reconciliation_required"
    assert old_after["result"]["checkpoint"] == "reconciliation_required"
    assert old_after["context"]["checkpoint_notes"] == {"preserve": "old history"}
    assert old_after["context"]["snapshot_supersession"] == {
        "reason": "stale_unsent_submission_replaced",
        "superseded_at": old_after["context"]["snapshot_supersession"]["superseded_at"],
        "replacement_worker_id": case["current_worker_id"],
        "saved_snapshot": {"jd_version": case["old_jd"], "profile_version": case["old_profile"]},
        "current_snapshot": {"jd_version": case["current_jd"], "profile_version": case["current_profile"]},
    }
    assert old_after["error"].startswith("Superseded by current-snapshot submission worker")
    assert store.get_agent_worker(case["current_worker_id"]) == current_before
    assert store.get_agent_worker(case["child_worker_id"]) == child_before
    inbox_after = next(item for item in store.agent_inbox(case["pipeline_id"])
                       if item["id"] == case["inbox_id"])
    assert inbox_after == inbox_before
    assert resumed["status"] == "awaiting_user"
    assert case["old_worker_id"] not in store.list_queued_agent_workers()
    with store.connect() as db:
        old_events_after = [dict(row) for row in db.execute(
            "SELECT id,worker_id,session_id,event_type,tool_name,success,payload,created_at "
            "FROM agent_runtime_events WHERE worker_id=? ORDER BY id", (case["old_worker_id"],))]
        supersession_events = [dict(row) for row in db.execute(
            "SELECT kind,payload FROM events WHERE job_id=? AND kind='agent.worker_superseded_by_current_snapshot'",
            (case["job_id"],))]
    assert old_events_after == old_events_before
    assert len(supersession_events) == 1
    audit = json.loads(supersession_events[0]["payload"])
    assert audit["worker_id"] == case["old_worker_id"]
    assert audit["replacement_worker_id"] == case["current_worker_id"]
    assert audit["saved_snapshot"] == {"jd_version": case["old_jd"], "profile_version": case["old_profile"]}
    assert audit["current_snapshot"] == {"jd_version": case["current_jd"], "profile_version": case["current_profile"]}


def test_resume_does_not_supersede_without_current_replacement(tmp_path):
    case = _seed_resume_snapshot_case(tmp_path, mismatch="jd", replacement=False)
    resumed = case["store"].resume_agent_pipeline(case["pipeline_id"])
    old = case["store"].get_agent_worker(case["old_worker_id"])
    assert old["state"] == "queued"
    assert "snapshot_supersession" not in old["context"]
    assert old["resume_mode"] == "same_session"
    assert resumed["status"] == "running"


def test_resume_keeps_possible_final_send_uncertain_even_with_current_replacement(tmp_path):
    case = _seed_resume_snapshot_case(tmp_path, mismatch="jd", possible_send=True)
    case["store"].resume_agent_pipeline(case["pipeline_id"])
    old = case["store"].get_agent_worker(case["old_worker_id"])
    assert old["state"] == "uncertain"
    assert old["context"]["final_action_may_have_been_sent"] is True
    assert "snapshot_supersession" not in old["context"]
    assert case["old_worker_id"] not in case["store"].list_queued_agent_workers()


def test_natural_completion_ignores_superseded_historical_submission(tmp_path):
    case = _seed_resume_snapshot_case(tmp_path, mismatch="jd")
    store = case["store"]
    store.resume_agent_pipeline(case["pipeline_id"])
    with store.connect(write=True) as db:
        db.execute("UPDATE agent_inbox SET state='approved',response=?,provenance='fixture review',resolved_at=? "
                   "WHERE id=?", (json.dumps({"approved": True}), "2026-01-02T00:00:00+00:00", case["inbox_id"]))
        db.execute("UPDATE agent_workers SET state='completed',turn_active=0,completed_at=? WHERE id=?",
                   ("2026-01-02T00:00:00+00:00", case["current_worker_id"]))
        db.execute("UPDATE pipeline_jobs SET agent_decision='approve',agent_status='submitted' "
                   "WHERE pipeline_id=? AND job_id=?", (case["pipeline_id"], case["job_id"]))
        db.execute("UPDATE jobs SET phase='submitted' WHERE id=?", (case["job_id"],))
        store._agent_refresh_pipeline(db, case["pipeline_id"])
    pipeline = store.get_pipeline(case["pipeline_id"])
    assert pipeline["status"] == "completed"
    assert store.get_agent_worker(case["old_worker_id"])["state"] == "superseded"


@pytest.mark.parametrize("title_focus", ["", "systems"])
def test_discovery_context_excludes_applicant_materials_but_evaluator_gets_them(tmp_path, title_focus):
    store = Store(tmp_path)
    source = tmp_path / "fixture-resume.md"
    source.write_text("# Test Candidate\n## Experience\nConfirmed fictional C++ experience.\n", encoding="utf-8")
    store.import_document("master_resume", source)
    pipeline = store.create_agent_pipeline("Fixture Context Scope", "https://fixture.example/careers",
                                          filters={"title_contains": title_focus})
    career_scope = pipeline["filters"]["career_scope"]
    assert "computer-science" in career_scope["required_scope"]
    assert career_scope["examples_are_non_exhaustive"] is True
    assert "semantic role focus" in career_scope["optional_title_focus"]
    discovery = store.list_agent_workers(pipeline["id"])[0]
    assert store.reserve_agent_worker(discovery["id"])
    discovery_launch = store.agent_launch_context(discovery["id"])
    discovery_initial = discovery_launch["task"]["task_context"]
    assert "applicant_index" not in discovery_initial
    assert discovery_initial["pipeline"]["filters"]["career_scope"] == career_scope
    assert discovery_initial["constraints"]["career_scope"] == career_scope

    # Simulate a pipeline created before the mandatory policy was persisted.
    # Future tool context must still carry the current scope without rewriting
    # the legacy filter snapshot or exposing applicant materials to discovery.
    legacy_filters = {"country": "US", "employment": "full_time",
                      "arrangement": ["onsite", "hybrid"], "title_contains": title_focus}
    with store.connect(write=True) as db:
        db.execute("UPDATE pipelines SET filters=? WHERE id=?",
                   (json.dumps(legacy_filters), pipeline["id"]))
    discovered = store.agent_action(discovery["id"], discovery_launch["capability"], "record_listing", {
        "requisition_id": "fixture-context-1", "url": "https://fixture.example/jobs/fixture-context-1",
        "title": "Fixture Context Engineer", "jd": "Fictional complete listing for scoped context test.",
        "metadata": {"location": "Seattle, WA", "country": "US", "employment": "full_time",
                     "arrangement": "hybrid"}, "selected": True,
        "selection_reason": "Selected in the isolated context fixture.",
    })
    discovery_context = store.agent_action(discovery["id"], discovery_launch["capability"], "get_context", {})
    assert discovery_context["sections"] == {}
    discovery_bundle = store.agent_action(discovery["id"], discovery_launch["capability"], "get_context",
                                         {"sections": ["pipeline"]})
    assert discovery_bundle["sections"]["pipeline"]["filters"] == {**legacy_filters, "career_scope": career_scope}
    assert discovery_bundle["constraints"]["career_scope"] == career_scope

    evaluator = next(worker for worker in store.list_agent_workers(pipeline["id"])
                     if worker["role"] == "evaluator")
    assert evaluator["job_id"] == discovered["job_id"]
    assert store.reserve_agent_worker(evaluator["id"])
    evaluation_launch = store.agent_launch_context(evaluator["id"])
    evaluation_task = evaluation_launch["task"]["task_context"]
    assert evaluation_task["pipeline"]["filters"]["career_scope"] == career_scope
    assert evaluation_task["constraints"]["career_scope"] == career_scope
    assert {"source_documents", "confirmed_knowledge", "technology_aliases", "answered_questions"}.issubset(
        evaluation_task["applicant_index"])
    assert "content" not in evaluation_task["applicant_index"]["source_documents"][0]
    evaluation_context = store.agent_action(evaluator["id"], evaluation_launch["capability"],
                                             "get_context", {})
    assert evaluation_context["sections"] == {}
    evaluation_bundle = store.agent_action(evaluator["id"], evaluation_launch["capability"],
                                           "get_context", {"sections": ["pipeline", "job"]})
    assert evaluation_bundle["sections"]["pipeline"]["filters"]["career_scope"] == career_scope
    assert evaluation_bundle["sections"]["job"]["jd"] == "Fictional complete listing for scoped context test."
    assert evaluation_bundle["constraints"]["career_scope"] == career_scope


def test_runtime_controls_refresh_live_quota_without_rewriting_worker_history(tmp_path):
    store = Store(tmp_path)
    pipeline = store.create_agent_pipeline("Fixture Live Runtime Controls", "https://fixture.example/careers")
    worker = store.list_agent_workers(pipeline["id"])[0]
    assert store.reserve_agent_worker(worker["id"])
    initial_launch = store.agent_launch_context(worker["id"])
    assert initial_launch["task"]["task_context"]["runtime_controls"]["quota_paused"] is False

    historical = {
        "disposition": "quota_paused",
        "provider_error": "The previous CLI turn reached its usage limit.",
        "session_id": "fixture-saved-session",
    }
    store.finish_agent_turn(worker["id"], historical)
    assert store.get_agent_worker(worker["id"])["state"] == "quota_paused"
    assert store.resume_agent_quota()["paused"] is False
    assert store.reserve_agent_worker(worker["id"])

    resumed_launch = store.agent_launch_context(worker["id"])
    launch_controls = resumed_launch["task"]["task_context"]["runtime_controls"]
    assert launch_controls["quota_paused"] is False
    assert launch_controls["quota"]["provider_error"] is None
    assert launch_controls["pipeline_status"] == "running"
    assert launch_controls["stop_requested"] is False
    assert launch_controls["worker_state"] == "running"
    assert launch_controls["checked_at"]
    # The prior quota response remains part of history and is not treated as
    # the current latch after an explicit quota resume.
    assert "result" not in resumed_launch["task"]["worker"]
    assert store.get_agent_worker(worker["id"])["result"]["provider_error"] == historical["provider_error"]

    token = resumed_launch["capability"]
    active_context = store.agent_action(worker["id"], token, "get_context", {})
    assert active_context["runtime_controls"]["quota_paused"] is False
    store.set_agent_quota(True, provider_error="A new current usage pause.", reason="Current pause fixture.")
    paused_context = store.agent_action(worker["id"], token, "get_context", {})
    assert paused_context["runtime_controls"]["quota_paused"] is True
    assert paused_context["runtime_controls"]["quota"]["provider_error"] == "A new current usage pause."
    store.set_agent_quota(False)
    cleared_context = store.agent_action(worker["id"], token, "get_context", {})
    assert cleared_context["runtime_controls"]["quota_paused"] is False
    assert store.get_agent_worker(worker["id"])["result"]["provider_error"] == historical["provider_error"]


def test_stop_cancels_reserved_worker_waiting_in_shared_executor_queue(tmp_path):
    store = Store(tmp_path)
    pipeline = store.create_agent_pipeline("Fixture Queued Stop", "https://fixture.example/careers")
    pool = ThreadPoolExecutor(max_workers=1)
    gate = threading.Event()
    blocker = pool.submit(gate.wait)
    manager = AgentPipelineManager(store, runtime_factory=lambda _store: None, executor=pool)
    try:
        view = manager.start(pipeline["id"])
        worker = store.list_agent_workers(pipeline["id"])[0]
        assert worker["state"] == "running" and worker["turn_active"] == 1
        assert worker["id"] in view["active_workers"]

        stopped = manager.stop(pipeline["id"])
        final_worker = store.get_agent_worker(worker["id"])
        assert stopped["pipeline"]["status"] == "stopped"
        assert final_worker["state"] == "stopped" and final_worker["turn_active"] == 0
    finally:
        gate.set()
        blocker.result(timeout=2)
        manager.shutdown()


def test_durable_reservations_enforce_pipeline_caps_and_job_local_waits(tmp_path):
    store = Store(tmp_path)
    pipeline = store.create_agent_pipeline("Fixture Concurrency Co", "https://fixture.example/careers")
    with pytest.raises(Conflict, match="already has an active"):
        store.create_agent_pipeline("Fixture Concurrency Co", "https://fixture.example/careers")

    discovery = store.list_agent_workers(pipeline["id"])[0]
    assert store.reserve_agent_worker(discovery["id"])
    discovery_launch = store.agent_launch_context(discovery["id"])
    for index in range(4):
        store.agent_action(discovery["id"], discovery_launch["capability"], "record_listing", {
            "requisition_id": f"fixture-cap-{index}",
            "url": f"https://fixture.example/jobs/fixture-cap-{index}",
            "title": f"Fixture Systems Engineer {index}",
            "jd": f"Fictional complete listing {index} for concurrency-cap coverage.",
            "metadata": {"location": "Seattle, WA", "country": "US",
                         "employment": "full_time", "arrangement": "hybrid"},
            "selected": True,
            "selection_reason": "Selected by the isolated concurrency fixture.",
        })

    evaluators = [worker for worker in store.list_agent_workers(pipeline["id"])
                  if worker["role"] == "evaluator"]
    assert len(evaluators) == 4
    for evaluator in evaluators[:3]:
        assert store.reserve_agent_worker(evaluator["id"])
    assert not store.reserve_agent_worker(evaluators[3]["id"])

    # A question releases this evaluator's slot after its saved CLI turn returns;
    # the other two jobs remain active and the fourth job can still advance.
    waiting_worker = evaluators[0]
    waiting_launch = store.agent_launch_context(waiting_worker["id"])
    question = store.agent_action(waiting_worker["id"], waiting_launch["capability"], "ask_question", {
        "question": "What is the applicant's exact confirmed answer to the fixture question?",
        "context": {"fixture": True},
    })
    store.finish_agent_turn(waiting_worker["id"], {"disposition": "waiting",
                                                   "checkpoint": f"inbox:{question['inbox_item_id']}"})
    assert store.get_agent_worker(waiting_worker["id"])["state"] == "waiting_question"
    assert store.get_agent_worker(waiting_worker["id"])["turn_active"] == 0
    assert store.reserve_agent_worker(evaluators[3]["id"])

    # Resolve the question and finish the other three evaluators. Each decision
    # creates one scoped submission worker, all under the same pipeline.
    store.answer_agent_inbox(question["inbox_item_id"], "Fixture answer", "Explicit isolated test response.")
    for evaluator in (evaluators[1], evaluators[2], evaluators[3]):
        launch = store.agent_launch_context(evaluator["id"])
        assert store.get_agent_worker(evaluator["id"])["state"] == "running"
        store.agent_action(evaluator["id"], launch["capability"], "finish_evaluation", {
            "decision": "approve", "reason": "Accepted by the isolated concurrency fixture."})
        store.finish_agent_turn(evaluator["id"], {"disposition": "completed"})
    assert store.reserve_agent_worker(waiting_worker["id"])
    resumed_launch = store.agent_launch_context(waiting_worker["id"])
    store.agent_action(waiting_worker["id"], resumed_launch["capability"], "finish_evaluation", {
        "decision": "approve", "reason": "Accepted after the fixture answer."})
    store.finish_agent_turn(waiting_worker["id"], {"disposition": "completed"})

    submissions = [worker for worker in store.list_agent_workers(pipeline["id"])
                   if worker["role"] == "submission"]
    assert len(submissions) == 4
    for submission in submissions[:3]:
        assert store.reserve_agent_worker(submission["id"])
    assert not store.reserve_agent_worker(submissions[3]["id"])

    # A document child uses its separate executor/claim and does not consume a
    # fourth submission slot; its waiting_child parent still retains one of the
    # three submission claims.
    parent = submissions[0]
    parent_launch = store.agent_launch_context(parent["id"])
    child_info = store.agent_action(parent["id"], parent_launch["capability"], "request_documents", {
        "artifacts": ["resume"], "reason": "The fictional form exposes a resume field.",
        "form_context": {"fixture": True},
    })
    store.finish_agent_turn(parent["id"], {"disposition": "waiting",
                                            "checkpoint": "waiting_child"})
    child = store.get_agent_worker(child_info["child_worker_id"])
    assert child["role"] == "document"
    assert store.reserve_agent_worker(child["id"])
    assert not store.reserve_agent_worker(submissions[3]["id"])

    # An independent pipeline has independent evaluator/submission caps.
    other = store.create_agent_pipeline("Fixture Independent Co", "https://other.fixture.example/careers")
    other_discovery = store.list_agent_workers(other["id"])[0]
    assert store.reserve_agent_worker(other_discovery["id"])
    other_launch = store.agent_launch_context(other_discovery["id"])
    store.agent_action(other_discovery["id"], other_launch["capability"], "record_listing", {
        "requisition_id": "independent-1", "url": "https://other.fixture.example/jobs/independent-1",
        "title": "Fixture Independent Engineer", "jd": "Fictional independent pipeline listing.",
        "metadata": {"location": "Seattle, WA", "country": "US", "employment": "full_time",
                     "arrangement": "onsite"}, "selected": True,
        "selection_reason": "Selected in the independent fixture pipeline.",
    })
    other_eval = next(worker for worker in store.list_agent_workers(other["id"])
                      if worker["role"] == "evaluator")
    assert store.reserve_agent_worker(other_eval["id"])


class ProductiveDiscoveryContinuationRuntime:
    """Two bounded discovery turns on one saved session; no browser/network."""

    def __init__(self, store):
        self.store = store
        self.turns = []

    def run(self, launch, on_event, should_stop):
        task = launch["task"]
        task["capability"] = launch["capability"]
        worker = task["worker"]
        session_id = worker.get("session_id") or "fixture-amazon-owned-session"
        self.turns.append({"worker_id": worker["id"], "session_id": session_id,
                           "resume_mode": worker["resume_mode"], "turn": len(self.turns) + 1})
        on_event({"type": "thread.resumed" if worker.get("session_id") else "thread.started",
                  "thread_id": session_id})
        assert not should_stop()

        turn = len(self.turns)
        context = {"tab_id": 757321209, "tab_url": "https://www.amazon.jobs/en/search",
                   "current_page": turn + 1, "pages": 17, "remaining_pages": 17 - turn,
                   "reviewed_requisitions": [f"fixture-req-{index}" for index in range(turn)]}
        self.store.agent_action(worker["id"], launch["capability"], "checkpoint", {
            "checkpoint": f"Amazon page {turn + 1} of 17; owned tab 757321209.",
            "context": context,
        })
        if turn == 1:
            self.store.agent_action(worker["id"], launch["capability"], "record_listing", {
                "requisition_id": "fixture-amazon-unknown-arrangement",
                "url": "https://www.amazon.jobs/en/jobs/fixture-amazon-unknown-arrangement",
                "title": "Software Development Engineer, Systems",
                "jd": "A complete fictional computer science listing whose arrangement is unstated.",
                "metadata": {"location": "Seattle, WA", "country": "US", "employment": "full_time"},
                "selected": False,
                "selection_reason": "Arrangement is unknown; preserve the listing for later review.",
            })
            result = self.store.agent_action(worker["id"], launch["capability"], "finish_discovery", {
                "status": "incomplete", "detail": "Page one was reviewed; more pages remain.",
                "coverage": {"results_count": 168, "pages": 17, "current_page": 2,
                             "remaining_pages": 16, "reviewed_requisitions": ["fixture-req-0"]},
            })
            assert result["continuation"] == {
                "state": "queued", "resume_mode": "same_session", "reason": "durable_progress"}
            on_event({"type": "tool_result", "tool_name": "agent_bridge.finish_discovery",
                      "success": True, "session_id": session_id})
            return {"disposition": "waiting", "session_id": session_id,
                    "checkpoint": "Amazon page 2 of 17; owned tab 757321209."}

        assert worker["resume_mode"] == "same_session"
        assert worker["session_id"] == "fixture-amazon-owned-session"
        result = self.store.agent_action(worker["id"], launch["capability"], "finish_discovery", {
            "status": "complete", "detail": "All relevant public search paths were reviewed.",
            "coverage": {"results_count": 168, "pages": 17, "current_page": 17,
                         "remaining_pages": 0, "reviewed_requisitions": ["fixture-req-0", "fixture-req-1"]},
        })
        assert result["status"] == "complete"
        on_event({"type": "tool_result", "tool_name": "agent_bridge.finish_discovery",
                  "success": True, "session_id": session_id})
        return {"disposition": "completed", "session_id": session_id,
                "checkpoint": "Discovery coverage complete."}


def test_productive_incomplete_discovery_continues_same_worker_and_session(tmp_path):
    store = Store(tmp_path)
    pipeline = store.create_agent_pipeline("Fixture Amazon Continuation", "https://amazon.fixture.example/careers")
    runtime = ProductiveDiscoveryContinuationRuntime(store)
    manager = AgentPipelineManager(store, runtime_factory=lambda _store: runtime)
    try:
        manager.start(pipeline["id"])
        def completed_pipeline():
            current = store.get_pipeline(pipeline["id"])
            return current if current["status"] == "completed" else None

        completed = _wait_for(completed_pipeline, timeout=10)
        workers = store.list_agent_workers(pipeline["id"])
        assert len(workers) == 1
        assert workers[0]["role"] == "discovery" and workers[0]["state"] == "completed"
        assert [turn["worker_id"] for turn in runtime.turns] == [workers[0]["id"], workers[0]["id"]]
        assert runtime.turns[1]["session_id"] == runtime.turns[0]["session_id"]
        assert runtime.turns[1]["resume_mode"] == "same_session"
        assert completed["discovery_status"] == "complete"
        jobs = store.pipeline_jobs(pipeline["id"])["jobs"]
        assert len(jobs) == 1
        assert jobs[0]["requisition_id"] == "fixture-amazon-unknown-arrangement"
        assert jobs[0]["selected"] is False
    finally:
        manager.shutdown()


def _begin_isolated_discovery_turn(store, company):
    pipeline = store.create_agent_pipeline(company, f"https://{company.casefold().replace(' ', '-')}.fixture.example/careers")
    worker = store.list_agent_workers(pipeline["id"])[0]
    assert store.reserve_agent_worker(worker["id"])
    launch = store.agent_launch_context(worker["id"])
    store.agent_runtime_event(worker["id"], {"type": "thread.started", "thread_id": "fixture-saved-session"})
    return pipeline, worker, launch


def _write_discovery_checkpoint(store, worker_id, token, *, include_tab=True, page=2, reviewed=None):
    context = {"current_page": page, "pages": 17, "remaining_pages": 17 - page,
               "reviewed_requisitions": list(reviewed or [])}
    if include_tab:
        context.update({"tab_id": 757321209, "tab_url": "https://amazon.fixture.example/search"})
    return store.agent_action(worker_id, token, "checkpoint", {
        "checkpoint": f"Fixture discovery page {page} of 17.", "context": context,
    })


def _record_unselected_discovery_listing(store, worker_id, token):
    return store.agent_action(worker_id, token, "record_listing", {
        "requisition_id": "fixture-unknown-arrangement",
        "url": "https://amazon.fixture.example/jobs/fixture-unknown-arrangement",
        "title": "Software Development Engineer, Systems",
        "jd": "Complete fictional CS job description; arrangement is not stated.",
        "metadata": {"location": "Seattle, WA", "country": "US", "employment": "full_time"},
        "selected": False, "selection_reason": "Unknown arrangement; preserved for later review.",
    })


def test_unchanged_incomplete_replay_requires_explicit_resume(tmp_path):
    store = Store(tmp_path)
    pipeline, worker, launch = _begin_isolated_discovery_turn(store, "Fixture Amazon No Progress")
    _write_discovery_checkpoint(store, worker["id"], launch["capability"], reviewed=["fixture-unknown-arrangement"])
    _record_unselected_discovery_listing(store, worker["id"], launch["capability"])
    coverage = {"current_page": 2, "pages": 17, "remaining_pages": 15,
                "reviewed_requisitions": ["fixture-unknown-arrangement"]}
    first = store.agent_action(worker["id"], launch["capability"], "finish_discovery", {
        "status": "incomplete", "detail": "More pages remain.", "coverage": coverage})
    assert first["continuation"]["state"] == "queued"
    store.finish_agent_turn(worker["id"], {"disposition": "waiting", "session_id": "fixture-saved-session"})
    assert store.get_agent_worker(worker["id"])["state"] == "queued"

    assert store.reserve_agent_worker(worker["id"])
    resumed_launch = store.agent_launch_context(worker["id"])
    assert resumed_launch["task"]["worker"]["session_id"] == "fixture-saved-session"
    assert resumed_launch["task"]["worker"]["resume_mode"] == "same_session"
    _write_discovery_checkpoint(store, worker["id"], resumed_launch["capability"],
                                reviewed=["fixture-unknown-arrangement"])
    second = store.agent_action(worker["id"], resumed_launch["capability"], "finish_discovery", {
        "status": "incomplete", "detail": "The same coverage is still incomplete.", "coverage": coverage})
    assert second["continuation"]["state"] == "interrupted"
    assert second["continuation"]["reason"] == "no_durable_progress"
    store.finish_agent_turn(worker["id"], {"disposition": "waiting", "session_id": "fixture-saved-session"})
    interrupted = store.get_agent_worker(worker["id"])
    assert interrupted["state"] == "interrupted"
    assert "without new listing or page progress" in interrupted["error"]
    assert worker["id"] not in store.list_queued_agent_workers()

    store.resume_agent_pipeline(pipeline["id"])
    explicitly_resumed = store.get_agent_worker(worker["id"])
    assert explicitly_resumed["state"] == "queued"
    assert explicitly_resumed["resume_mode"] == "same_session"


def test_incomplete_discovery_with_progress_but_missing_owned_tab_requires_resume(tmp_path):
    store = Store(tmp_path)
    pipeline, worker, launch = _begin_isolated_discovery_turn(store, "Fixture Amazon Missing Tab")
    _write_discovery_checkpoint(store, worker["id"], launch["capability"], include_tab=False,
                                page=2, reviewed=["fixture-new"])
    _record_unselected_discovery_listing(store, worker["id"], launch["capability"])
    result = store.agent_action(worker["id"], launch["capability"], "finish_discovery", {
        "status": "incomplete", "detail": "A listing was recorded; more pages remain.",
        "coverage": {"current_page": 2, "remaining_pages": 15},
    })
    assert result["continuation"]["state"] == "interrupted"
    assert result["continuation"]["reason"] == "missing_owned_tab"
    store.finish_agent_turn(worker["id"], {"disposition": "waiting", "session_id": "fixture-saved-session"})
    interrupted = store.get_agent_worker(worker["id"])
    assert interrupted["state"] == "interrupted"
    assert "lacks a valid owned tab and URL" in interrupted["error"]
    assert worker["id"] not in store.list_queued_agent_workers()
    store.resume_agent_pipeline(pipeline["id"])
    assert store.get_agent_worker(worker["id"])["state"] == "queued"


def test_blocked_discovery_detail_survives_runtime_waiting_disposition(tmp_path):
    store = Store(tmp_path)
    pipeline, worker, launch = _begin_isolated_discovery_turn(store, "Fixture Amazon Blocked")
    blocked = store.agent_action(worker["id"], launch["capability"], "finish_discovery", {
        "status": "blocked", "detail": "The site requires an unavailable sign-in checkpoint.",
        "coverage": {"pages": 1, "remaining_pages": 16},
    })
    assert blocked["detail"] == "The site requires an unavailable sign-in checkpoint."
    store.finish_agent_turn(worker["id"], {"disposition": "waiting", "session_id": "fixture-saved-session"})
    final_worker = store.get_agent_worker(worker["id"])
    assert final_worker["state"] == "failed"
    assert final_worker["error"] == "The site requires an unavailable sign-in checkpoint."
    assert final_worker["id"] not in store.list_queued_agent_workers()
    blocked_pipeline = store.get_pipeline(pipeline["id"])
    assert blocked_pipeline["discovery_status"] == "blocked"
    assert any("Discovery needs attention" in note and
               "The site requires an unavailable sign-in checkpoint." in note
               for note in blocked_pipeline["blockers"])


@pytest.mark.parametrize(("barrier", "expected_state"), [
    ("question", "waiting_question"),
    ("approval", "waiting_approval"),
    ("quota", "quota_paused"),
    ("stop", "stopped"),
])
def test_discovery_wait_barriers_do_not_become_progress_continuations(tmp_path, barrier, expected_state):
    store = Store(tmp_path)
    pipeline, worker, launch = _begin_isolated_discovery_turn(store, f"Fixture Amazon {barrier}")
    _write_discovery_checkpoint(store, worker["id"], launch["capability"], page=2)
    if barrier == "question":
        store.agent_action(worker["id"], launch["capability"], "ask_question", {
            "question": "Which exact public listing page should be checked next?", "context": {"fixture": True},
        })
    elif barrier == "approval":
        store.agent_action(worker["id"], launch["capability"], "request_approval", {
            "action": "captcha", "summary": "The site displays a CAPTCHA checkpoint.",
            "observation": {"url": "https://amazon.fixture.example/search"},
        })
    elif barrier == "quota":
        store.agent_runtime_event(worker["id"], {"type": "quota_error", "message": "Fixture quota pause."})
    else:
        store.request_agent_pipeline_stop(pipeline["id"])

    store.finish_agent_turn(worker["id"], {
        "disposition": "quota_paused" if barrier == "quota" else "waiting",
        "session_id": "fixture-saved-session",
    })
    final_worker = store.get_agent_worker(worker["id"])
    assert final_worker["state"] == expected_state
    assert worker["id"] not in store.list_queued_agent_workers()

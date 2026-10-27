from __future__ import annotations

import base64
import hashlib
from pathlib import Path

from app.store import Store
from app.rendering import render_agent_documents
from tools.agent_mcp import AgentMCPBridge, render_and_register_documents


def _make_document_child(store: Store):
    pipeline = store.create_agent_pipeline("Fixture Renderer Co", "https://fixture.example/careers")
    discovery = store.list_agent_workers(pipeline["id"])[0]
    assert store.reserve_agent_worker(discovery["id"])
    discovery_launch = store.agent_launch_context(discovery["id"])
    store.agent_action(discovery["id"], discovery_launch["capability"], "record_listing", {
        "requisition_id": "renderer-fixture-1",
        "url": "https://fixture.example/jobs/renderer-fixture-1",
        "title": "Fictional Test Candidate",
        "jd": "A fictional test role for exercising the isolated document renderer.",
        "metadata": {"location": "Seattle, WA", "country": "US",
                     "employment": "full_time", "arrangement": "hybrid"},
        "selected": True,
        "selection_reason": "Selected by the deterministic renderer integration fixture.",
        "observation": {"source": "isolated integration test"},
    })
    evaluator = next(worker for worker in store.list_agent_workers(pipeline["id"])
                     if worker["role"] == "evaluator")
    assert store.reserve_agent_worker(evaluator["id"])
    evaluation_launch = store.agent_launch_context(evaluator["id"])
    store.agent_action(evaluator["id"], evaluation_launch["capability"], "finish_evaluation", {
        "decision": "approve", "reason": "The isolated fictional listing is approved for renderer testing."})
    parent = next(worker for worker in store.list_agent_workers(pipeline["id"])
                  if worker["role"] == "submission")
    assert store.reserve_agent_worker(parent["id"])
    parent_launch = store.agent_launch_context(parent["id"])
    child_info = store.agent_action(parent["id"], parent_launch["capability"], "request_documents", {
        "artifacts": ["resume"],
        "reason": "The fictional application fixture requests a resume.",
        "form_context": {"fixture": "renderer integration"},
    })
    child = store.get_agent_worker(child_info["child_worker_id"])
    assert store.reserve_agent_worker(child["id"])
    child_launch = store.agent_launch_context(child["id"])
    return pipeline, parent, child, child_launch


def test_published_document_is_rendered_registered_and_returned_to_parent(tmp_path):
    store = Store(tmp_path)
    pipeline, parent, child, child_launch = _make_document_child(store)
    bridge = AgentMCPBridge(
        child["id"], child_launch["capability"], "document", store.agent_action,
        document_renderer=lambda published: render_and_register_documents(
            store, child["id"], child_launch["capability"], published),
    )
    markdown = (
        "# Test Candidate\n\n"
        "candidate@example.test\n\n"
        "## Experience\n\n"
        "- Built fictional software systems for a local renderer integration test.\n"
    )
    value, images = bridge.invoke("publish_documents", {
        "artifacts": [{"kind": "resume", "markdown": markdown,
                       "source_notes": {"fixture": "fictional integration data"}}],
    })

    published = value["artifacts"][0]
    saved = store.get_agent_artifact(published["id"])
    upload = Path(saved["upload_path"])
    assert saved["status"] == "draft"
    assert upload.name == "resume.pdf"
    assert upload.parent.name == "rendered"
    assert upload.read_bytes().startswith(b"%PDF-")
    assert hashlib.sha256(upload.read_bytes()).hexdigest() == saved["pdf_sha256"]
    assert saved["sha256"] == published["sha256"]
    assert saved["preview_paths"]
    assert value["rendered_previews"][0]["sha256"] == saved["pdf_sha256"]
    assert images and images[0]["mimeType"] == "image/png"
    assert base64.b64decode(images[0]["data"]).startswith(b"\x89PNG\r\n\x1a\n")

    completed = store.finish_agent_turn(child["id"], {"disposition": "completed"})
    assert completed["state"] == "completed"
    assert store.get_agent_artifact(published["id"])["status"] == "ready"
    assert store.reserve_agent_worker(parent["id"])
    launch = store.agent_launch_context(parent["id"])
    resumed = store.agent_action(parent["id"], launch["capability"], "get_context",
                                 {"sections": ["prepared_documents"]})["sections"]["prepared_documents"]
    assert len(resumed) == 1
    assert resumed[0]["markdown"] == markdown
    assert resumed[0]["upload_path"] == str(upload.resolve())
    assert resumed[0]["upload_filename"] == "resume.pdf"
    assert resumed[0]["upload_sha256"] == saved["pdf_sha256"]
    assert resumed[0]["preview_paths"] == saved["preview_paths"]


def test_agent_renderer_rejects_package_path_escape(tmp_path):
    store = Store(tmp_path)
    outside = tmp_path.parent / "outside-agent-package"
    outside.mkdir()
    published = {
        "package_id": "..\\outside-agent-package",
        "artifacts": [{"id": "fixture", "kind": "resume", "filename": "resume.md",
                       "path": str(outside / "resume.md"), "sha256": "0" * 64}],
    }

    try:
        render_agent_documents(store, published)
    except ValueError as error:
        assert "outside the agent artifact workspace" in str(error)
    else:
        raise AssertionError("Renderer accepted a package path outside agent artifacts.")

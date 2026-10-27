"""Opt-in real subscription test: synthetic documents, same-thread continuation.

No browser, upload, employer, production ledger, credentials or API key access.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.app_server_runtime import CodexAppServerRuntime
from app.store import Store


def run():
    root = ROOT / "data/efficiency-smoke" / uuid.uuid4().hex
    (root / "config").mkdir(parents=True)
    (root / "prompts").mkdir()
    (root / "config/workers.json").write_text(json.dumps({"authentication": "chatgpt", "models": {
        "materials": {"model": "gpt-6-luna", "effort": "high"}}}), encoding="utf-8")
    markdown = "# Test Candidate\n\ncandidate@example.test\n\n## Experience\n\n- Built fictional C++ software for a synthetic local test.\n"
    instructions = ("Synthetic local document-host test. Use only agent_bridge tools. No other tools, browser, shell, "
                    "network, personal files, employer contact or real applicant data. First get_context({}); "
                    "then publish_documents with exactly one resume artifact whose markdown is " + json.dumps(markdown) +
                    ". Source notes: fictional fixture. Inspect the actual returned page image for layout/content. "
                    "Then checkpoint with label smoke_reviewed and context {fixture: true}. Return the required "
                    "JSON disposition=waiting, checkpoint=smoke_reviewed, summary describing only your actual observations.")
    (root / "prompts/document_agent.md").write_text(instructions, encoding="utf-8")
    store = Store(root)
    pipeline = store.create_agent_pipeline("SYNTHETIC HOST TEST", "https://fictional.invalid/careers")
    job = store.discover(pipeline["company_id"], {"url": "https://fictional.invalid/job", "title": "Synthetic C++ role",
        "jd": "Fictional document host test only.", "location": "Austin", "country": "US", "employment": "full_time", "arrangement": "hybrid"})
    store.add_pipeline_job(pipeline["id"], job["id"])
    with store.connect(write=True) as db:
        parent = store._agent_insert_worker(db, pipeline["id"], "submission", job_id=job["id"])
    store.reserve_agent_worker(parent)
    parent_launch = store.agent_launch_context(parent)
    child = store.agent_action(parent, parent_launch["capability"], "request_documents",
                              {"artifacts": ["resume"], "reason": "Synthetic form field", "form_context": {"test_only": True}})["child_worker_id"]
    host = CodexAppServerRuntime(root, store=store)
    reports, usage, started = [], [], time.monotonic()

    def event(value):
        store.agent_runtime_event(child, value)
        if value.get("type") == "usage":
            store.record_agent_usage(child, value)
            usage.append(value)

    try:
        assert store.reserve_agent_worker(child)
        result = host.run(store.agent_launch_context(child), event, lambda: time.monotonic() - started > 120)
        reports.append(result)
        if result.get("disposition") != "waiting":
            raise RuntimeError("First synthetic document turn did not complete its planned review wait: " + str(result))
        store.finish_agent_turn(child, result)
        first_session = store.get_agent_worker(child)["session_id"]
        (root / "prompts/document_agent.md").write_text(instructions.replace("disposition=waiting", "disposition=completed"), encoding="utf-8")
        store.resume_agent_pipeline(pipeline["id"])
        assert store.reserve_agent_worker(child)
        second = host.run(store.agent_launch_context(child), event, lambda: time.monotonic() - started > 180)
        reports.append(second)
        if second.get("disposition") != "completed" or second.get("session_id") != first_session:
            raise RuntimeError("Same-thread synthetic continuation failed: " + str(second))
        store.finish_agent_turn(child, second)
        metrics = store.get_agent_efficiency(pipeline["id"])
        host.close()
        restarted = CodexAppServerRuntime(root, store=store)
        try:
            restarted._ensure()
            resumed = restarted._request("thread/resume", {"threadId": first_session.removeprefix("app_server:"), "excludeTurns": True})
            restart_verified = resumed["thread"]["id"] == first_session.removeprefix("app_server:")
        finally:
            restarted.close()
        report = {"test_only": True, "workspace": str(root), "turns": reports, "usage": usage,
                  "metrics": metrics, "same_session": True, "restart_resume_no_inference": restart_verified,
                  "browser_support": "unverified", "employer_actions": 0}
    except Exception as error:
        report = {"test_only": True, "workspace": str(root), "turns": reports, "usage": usage, "error": str(error),
                  "employer_actions": 0}
    finally:
        host.close()
    path = ROOT / "data/integration/20261023-app-server-smoke.json"
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(path), "same_session": report.get("same_session"),
                      "restart_resume": report.get("restart_resume_no_inference"), "error": report.get("error")}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-subscription", action="store_true")
    if not parser.parse_args().run_subscription:
        parser.error("Explicit --run-subscription is required.")
    run()

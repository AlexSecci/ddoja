"""Opt-in integrated DeepSeek/Playwright acceptance on a local fictional listing.

Uses the configured paid provider only when --run-paid-test is supplied. The
production ledger, campaigns, model preferences and browser profiles are untouched.
"""
from __future__ import annotations

import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import sys
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.agent_runtime import CodexAgentRuntime
from app.store import Store
from app.provider_credentials import ProviderCredentials
from tools.agentic_fixture_harness import _copy_runtime_tree


class FictionalListing(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        if self.path == "/favicon.ico":
            self.send_response(204); self.end_headers(); return
        if self.path != "/synthetic-job":
            self.send_error(404); return
        page = ("<!doctype html><title>Synthetic job</title><h1>Fictional C++ Engineer</h1>"
                "<p>Requisition DS-FIXTURE-1. Austin, Texas. Full time, hybrid.</p>"
                "<article><p>BEGIN SYNTHETIC DESCRIPTION</p><p>Build fictional C++ services and automation. "
                "Debug concurrency and IPC. This is a local acceptance fixture, not a real vacancy.</p>"
                "<p>END SYNTHETIC DESCRIPTION</p></article>")
        self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.end_headers()
        self.wfile.write(page.encode())


def run():
    root = ROOT / "data/provider-smoke" / uuid.uuid4().hex
    _copy_runtime_tree(root)
    shutil.copy2(ROOT / "config/deepseek-models.json", root / "config/deepseek-models.json")
    shutil.copytree(ROOT / "browser-runtime/node_modules", root / "browser-runtime/node_modules")
    # Same Windows user can decrypt this isolated copy; it is never logged.
    credential = ProviderCredentials(ROOT)
    ProviderCredentials(root).save(credential.get())
    server = ThreadingHTTPServer(("127.0.0.1", 0), FictionalListing)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}/synthetic-job"
    instructions = (
        "This is an explicitly authorized synthetic provider integration test. Use only agent_bridge and playwright tools. "
        "No employer site, real applicant data, login, upload, CAPTCHA, agreement or application send is in scope. "
        "First get_context. Open only " + url + ". Read its browser_snapshot inline. Capture the single full fictional JD "
        "using capture_listing_batch and actual text-block markers BEGIN SYNTHETIC DESCRIPTION and END SYNTHETIC DESCRIPTION; "
        "title Fictional C++ Engineer, requisition DS-FIXTURE-1, exact URL above. Set selected=false because this is only a local fixture, "
        "metadata country US, employment full_time, arrangement hybrid, location Austin Texas. Do not invent a description pointer. "
        "Verify the tool recorded the job. Close this dedicated browser. Then finish_discovery status complete, "
        "detail Single fictional page read and recorded, coverage {synthetic:true,pages_reviewed:1,remaining:[]}. "
        "Return required JSON disposition completed only after the durable finish succeeds."
    )
    (root / "prompts/discovery_agent.md").write_text(instructions, encoding="utf-8")
    (root / "prompts/agent_core.md").write_text("Follow the exact fictional test scope and report actual tool results honestly.", encoding="utf-8")
    store = Store(root)
    settings = store.get_worker_settings()
    settings["roles"]["discovery"] = {"provider": "deepseek", "model": "deepseek-flash", "effort": "low", "browser_backend": "playwright"}
    store.save_worker_settings(0, settings["roles"])
    pipeline = store.create_agent_pipeline("SYNTHETIC PROVIDER ACCEPTANCE", url, allow_http_loopback_test=True, test_only=True)
    worker = store.list_agent_workers(pipeline["id"])[0]
    assert store.reserve_agent_worker(worker["id"])
    started = time.monotonic()

    def event(value):
        store.agent_runtime_event(worker["id"], value)
        if value.get("type") == "usage":
            store.record_agent_usage(worker["id"], value)

    try:
        result = CodexAgentRuntime(root).run(store.agent_launch_context(worker["id"]), event, lambda: time.monotonic() - started > 100)
        store.finish_agent_turn(worker["id"], result)
        with store.connect() as db:
            jobs = db.execute("SELECT j.requisition_id,jd.jd FROM jobs j JOIN job_versions jd ON jd.job_id=j.id AND jd.version=j.jd_version").fetchall()
            captures = db.execute("SELECT COUNT(*) FROM agent_listing_captures WHERE capture_ref!=''").fetchone()[0]
        valid = (result.get("disposition") == "completed" and len(jobs) == 1 and jobs[0]["requisition_id"] == "DS-FIXTURE-1"
                 and "Debug concurrency and IPC" in jobs[0]["jd"] and captures == 1)
        report = {"passed": valid, "synthetic": True, "root": str(root), "result": result,
                  "browser_captures": captures, "usage": store.get_agent_efficiency(pipeline["id"])}
        (root / "acceptance.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps({"passed": valid, "root": str(root), "disposition": result.get("disposition"),
                          "captures": captures, "timing": {k: result.get("timing", {}).get(k) for k in
                          ("worker_elapsed_seconds", "browser_tool_seconds", "other_tool_seconds", "model_and_transport_seconds")},
                          "error": result.get("error")}))
        return 0 if valid else 1
    finally:
        server.shutdown(); server.server_close()
        ProviderCredentials(root).remove()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-paid-test", action="store_true", required=True)
    parser.parse_args()
    raise SystemExit(run())

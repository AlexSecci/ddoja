"""Opt-in, isolated server for the rendered pipeline UI acceptance check.

This harness serves the repository's real static UI and FastAPI routes on
127.0.0.1:8765, but uses a temporary SQLite workspace. Its pipeline manager is
the production PipelineManager with its real executor/start/stop lifecycle.
Only external work adapters are replaced: discovery produces synthetic
``.invalid`` listings and waits at a stop-aware checkpoint; model workers and
submission fail closed. The fictional browser-practice endpoint is also
unavailable because the Store root is not the application workspace.

Parent-controlled run procedure:
1. Wait until the approved browser-practice attempt is finished and Chrome is
   released.
2. Stop the normal service in its owning terminal.
3. Run ``python tools/ui_acceptance_harness.py --serve`` in the project folder.
4. Exercise only the explicitly labeled UI TEST FIXTURE companies and records.
5. Stop this harness with Ctrl+C, confirm its temporary store is removed, then
   restore the normal service from its owning terminal.

Do not point this harness at the real data directory or use it for an employer
application. It deliberately has no option to select a workspace or port.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
import sys
import tempfile
import threading
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from fastapi.testclient import TestClient

from app import pipelines as pipeline_module
from app import server as server_module
from app import submission as submission_module
from app.pipelines import AgentPipelineManager
from app.server import ROOT, create_app
from app.store import Store, timestamp


FIXTURE_PREFIX = "ui-test-fixture-"
FAKE_DISCOVERY_MAX_SECONDS = 600


class NoExternalAgentRuntime:
    """Fail closed if an acceptance fixture accidentally launches agent work."""

    def run(self, *_args, **_kwargs):
        raise AssertionError("UI TEST FIXTURE must not launch a model, browser, or external agent runtime.")


class ApiRequestCounter:
    """Count only local API requests while preserving the wrapped ASGI lifespan."""

    def __init__(self, app):
        self.app = app
        self.lock = threading.Lock()
        self.counts = Counter()

    def record(self, method, path):
        # Normalize per-pipeline IDs so repeated refreshes aggregate into a
        # small, readable request profile.
        pieces = str(path).strip("/").split("/")
        if len(pieces) >= 3 and pieces[:2] == ["api", "pipelines"]:
            pieces[2] = "{pipeline_id}"
            if len(pieces) >= 5 and pieces[3] == "jobs":
                pieces[4] = "{job_id}"
            if len(pieces) >= 5 and pieces[3] == "workers":
                pieces[4] = "{worker_id}"
            if len(pieces) >= 5 and pieces[3] == "inbox":
                pieces[4] = "{item_id}"
        key = str(method).upper() + " /" + "/".join(pieces)
        with self.lock:
            self.counts[key] += 1

    def snapshot(self, *, reset=False):
        with self.lock:
            result = dict(self.counts)
            if reset:
                self.counts.clear()
            return result


def _company(store: Store, key: str, label: str) -> str:
    company_id = f"{FIXTURE_PREFIX}{key}"
    careers_url = f"https://boards.greenhouse.io/{company_id}"
    store.company(company_id, f"UI TEST FIXTURE — {label}", careers_url, enabled=True)
    # This is a synthetic source record. The test-only discovery adapter below
    # never opens this URL or contacts the Greenhouse API.
    store.source(company_id, "greenhouse", company_id)
    return company_id


def _record(company_id: str, key: str, title: str, location: str, *, arrangement="onsite") -> dict:
    requisition = f"UIFIX-{key.upper()}"
    return {
        "requisition_id": requisition,
        "url": f"https://ui-test.invalid/{company_id}/{key}",
        "title": title,
        "jd": "UI TEST FIXTURE ONLY. Synthetic listing used to verify dashboard rendering.",
        "location": location,
        "country": "US",
        "employment": "full_time",
        "arrangement": arrangement,
        "source": "UI TEST FIXTURE — no employer feed was contacted.",
    }


def _fixture_job(store: Store, company_id: str, key: str, title: str, location: str, *, arrangement="onsite") -> str:
    return store.discover(company_id, _record(company_id, key, title, location, arrangement=arrangement))["id"]


def seed_display_fixtures(root: Path) -> dict[str, str]:
    """Seed clearly marked card fixtures in a temporary Store only."""
    store = Store(root)

    # These have supported-looking but entirely synthetic sources and no
    # existing runs. Enter these exact company names/URLs in the rendered form
    # to exercise pipeline creation without colliding with card fixtures.
    active_a = _company(store, "active-a", "Active A — fresh pipeline test")
    active_b = _company(store, "active-b", "Active B — independent stop test")

    resolved_company = _company(store, "resolved", "Resolved completion and outcomes")
    resolved = store.create_pipeline(resolved_company, "UI TEST FIXTURE", "Austin")
    submitted_job = _fixture_job(store, resolved_company, "resolved-submitted", "UI TEST FIXTURE Software Engineer", "Austin, TX")
    discarded_job = _fixture_job(store, resolved_company, "resolved-discarded", "UI TEST FIXTURE QA Engineer", "Austin, TX", arrangement="hybrid")
    excluded_job = _fixture_job(store, resolved_company, "resolved-excluded", "UI TEST FIXTURE Remote Role", "Austin, TX", arrangement="remote")
    store.add_pipeline_job(resolved["id"], submitted_job, selected=True)
    store.add_pipeline_job(resolved["id"], discarded_job, selected=True)
    store.add_pipeline_job(resolved["id"], excluded_job, selected=False,
                           excluded_reason="UI TEST FIXTURE: remote work is outside the fixed requirement.")

    receipt_path = root / "fixture-input" / "synthetic-receipt.txt"
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.write_text(
        "UI TEST FIXTURE — synthetic record only.\n"
        "Your Application Has Been Received.\n"
        "UI TEST FIXTURE Software Engineer Req UIFIX-RESOLVED-SUBMITTED\n",
        encoding="utf-8",
    )
    store.import_submission(submitted_job, receipt_path, "receipt", "UI TEST FIXTURE synthetic receipt",
                           attribution="synthetic UI test data")
    store.discard_pipeline_job(discarded_job, resolved["id"],
                               "UI TEST FIXTURE: intentionally resolved for display coverage.",
                               "UI TEST FIXTURE: seeded in the isolated temporary workspace.")
    store.update_pipeline(resolved["id"], discovery_status="complete",
                          discovery={"status": "complete", "detail": "UI TEST FIXTURE: synthetic discovery complete."})
    store.finalize_pipeline(resolved["id"], status="completed", stage="completed", blockers=[])

    incomplete_company = _company(store, "incomplete-discovery", "Incomplete discovery status")
    incomplete_discovery = store.create_pipeline(incomplete_company, "UI TEST FIXTURE", "Austin")
    incomplete_job = _fixture_job(store, incomplete_company, "incomplete-discovery-job",
                                  "UI TEST FIXTURE Engineer", "Austin, TX")
    store.add_pipeline_job(incomplete_discovery["id"], incomplete_job)
    store.update_pipeline(
        incomplete_discovery["id"], status="completed", stage="completed", discovery_status="incomplete",
        discovery={"status": "incomplete", "detail": "UI TEST FIXTURE: discovery intentionally incomplete."},
        blockers=["UI TEST FIXTURE: incomplete discovery state is intentional."], finished=True,
    )

    unresolved_company = _company(store, "unresolved-disposition", "Unresolved disposition status")
    unresolved = store.create_pipeline(unresolved_company, "UI TEST FIXTURE", "Austin")
    unresolved_job = _fixture_job(store, unresolved_company, "unresolved-disposition-job",
                                  "UI TEST FIXTURE Engineer", "Austin, TX")
    store.add_pipeline_job(unresolved["id"], unresolved_job)
    store.update_pipeline(
        unresolved["id"], status="completed", stage="completed", discovery_status="complete",
        discovery={"status": "complete", "detail": "UI TEST FIXTURE: discovery complete; one selected role unresolved."},
        blockers=["UI TEST FIXTURE: unresolved selected role is intentional."], finished=True,
    )

    return {"active_company_a": active_a, "active_company_b": active_b,
            "resolved_pipeline": resolved["id"], "submitted_job": submitted_job,
            "incomplete_discovery_pipeline": incomplete_discovery["id"],
            "unresolved_pipeline": unresolved["id"]}


def seed_agentic_ui_fixtures(root: Path) -> dict[str, str]:
    """Seed current agentic cards and inbox data without scheduling a runtime."""
    store = Store(root)
    active = store.create_agent_pipeline(
        "UI TEST FIXTURE — Active poll card", "https://active-poll.ui-test.invalid/careers",
        {"title_contains": "", "location_contains": ""},
    )

    inbox_pipeline = store.create_agent_pipeline(
        "UI TEST FIXTURE — Inbox draft", "https://inbox.ui-test.invalid/careers",
        {"title_contains": "", "location_contains": ""},
    )
    listing = store.discover(inbox_pipeline["company_id"], {
        "url": "https://inbox.ui-test.invalid/jobs/ui-question-1",
        "requisition_id": "UI-QUESTION-1",
        "title": "UI TEST FIXTURE Systems Engineer",
        "jd": "Synthetic question fixture only. No employer page or applicant data is used.",
        "location": "Austin, TX", "country": "US", "employment": "full_time",
        "arrangement": "hybrid",
    })
    store.add_pipeline_job(inbox_pipeline["id"], listing["id"], selected=True)
    with store.connect(write=True) as db:
        discovery_worker = db.execute(
            "SELECT * FROM agent_workers WHERE pipeline_id=? AND role='discovery'",
            (inbox_pipeline["id"],),
        ).fetchone()
        db.execute("UPDATE agent_workers SET state='completed',turn_active=0,completed_at=?,updated_at=? WHERE id=?",
                   (timestamp(), timestamp(), discovery_worker["id"]))
        evaluator_id = store._agent_insert_worker(db, inbox_pipeline["id"], "evaluator", job_id=listing["id"])
        evaluator = db.execute("SELECT * FROM agent_workers WHERE id=?", (evaluator_id,)).fetchone()
        question, _created = store._agent_insert_inbox(
            db, evaluator, "question", dedupe_key="ui-test-fixture-question",
            question="UI TEST FIXTURE ONLY: Which example location should this synthetic record show?",
            context={"test_only": True, "scope": "isolated dashboard draft-preservation check"},
            response_hint="Enter any synthetic fixture text; do not use real applicant information.",
        )
        db.execute("UPDATE pipelines SET status='awaiting_user',stage='evaluation',discovery_status='complete',"
                   "discovery_payload=?,updated_at=? WHERE id=?",
                   (json.dumps({"status": "complete", "detail": "UI TEST FIXTURE: one synthetic listing."}),
                    timestamp(), inbox_pipeline["id"]))

    recovery = store.create_agent_pipeline(
        "UI TEST FIXTURE — Saved worker recovery", "https://recovery.ui-test.invalid/careers",
        {"title_contains": "", "location_contains": ""},
    )
    recovery_worker = store.list_agent_workers(recovery["id"])[0]
    with store.connect(write=True) as db:
        db.execute("UPDATE agent_workers SET state='interrupted',session_id='ui-test-saved-session',"
                   "resume_mode='same_session',checkpoint='UI TEST FIXTURE checkpoint',"
                   "error='UI TEST FIXTURE: saved session is displayed only; no agent was launched.',"
                   "turn_active=0,updated_at=? WHERE id=?", (timestamp(), recovery_worker["id"]))
        db.execute("UPDATE pipelines SET status='interrupted',stage='discovery',discovery_status='incomplete',"
                   "discovery_payload=?,updated_at=? WHERE id=?",
                   (json.dumps({"status": "incomplete", "detail": "UI TEST FIXTURE: saved worker state.",
                                "coverage": {"results_count": 0, "remaining": "fixture is paused"},
                                "continuation": {"state": "interrupted", "resume_mode": "same_session",
                                                 "reason": "UI TEST FIXTURE saved state."}}),
                    timestamp(), recovery["id"]))
    return {"active_pipeline": active["id"], "inbox_pipeline": inbox_pipeline["id"],
            "inbox_job": listing["id"], "inbox_item": question["id"],
            "inbox_worker": evaluator_id, "recovery_pipeline": recovery["id"],
            "recovery_worker": recovery_worker["id"]}


def _isolated_app(root: Path):
    return create_app(root, runtime_factory=lambda _store: NoExternalAgentRuntime())


def _install_request_counter(app):
    counter = ApiRequestCounter(app)
    @app.middleware("http")
    async def count_api_requests(request, call_next):
        if request.url.path.startswith("/api/"):
            counter.record(request.method, request.url.path)
        return await call_next(request)

    @app.get("/__ui_test/request-counts")
    def request_counts():
        return {"requests": counter.snapshot()}

    return counter


def fake_discovery(store: Store, company_id: str, *, pipeline_id: str | None = None, **_kwargs) -> dict:
    """Persist deterministic fixture listings, then wait for a real manager stop."""
    if not pipeline_id or not company_id.startswith(FIXTURE_PREFIX):
        raise RuntimeError("UI TEST FIXTURE discovery refused a non-fixture company or pipeline.")
    pipeline = store.get_pipeline(pipeline_id)
    filters = pipeline["filters"]
    title_filter = (filters.get("title_contains") or "UI TEST FIXTURE Engineer").strip()
    location_filter = (filters.get("location_contains") or "Austin").strip()
    exact_location = f"{location_filter}, UI TEST FIXTURE"
    candidates = [
        _record(company_id, f"{pipeline_id[:8]}-match", f"{title_filter} UI TEST FIXTURE Role", exact_location),
        _record(company_id, f"{pipeline_id[:8]}-location-miss", f"{title_filter} UI TEST FIXTURE Role", "Raleigh, UI TEST FIXTURE", arrangement="hybrid"),
        _record(company_id, f"{pipeline_id[:8]}-title-miss", "Unrelated UI TEST FIXTURE Role", exact_location),
    ]
    counts = {"new": 0, "changed": 0, "unchanged": 0, "observed_posts": 0,
              "pages": 1, "selected_posts": 0, "excluded_posts": 0}
    title_needle = str(filters.get("title_contains") or "").casefold()
    location_needle = str(filters.get("location_contains") or "").casefold()
    for record in candidates:
        result = store.discover(company_id, record)
        counts["new" if result["created"] else "changed" if result["changed"] else "unchanged"] += 1
        selected, excluded_reason = True, ""
        if title_needle and title_needle not in record["title"].casefold():
            selected, excluded_reason = False, f"Title does not match: {filters['title_contains']}"
        location = record["location"].strip()
        if selected and location_needle and location_needle not in location.casefold():
            selected, excluded_reason = False, f"Location does not match: {filters['location_contains']}"
        store.add_pipeline_job(pipeline_id, result["id"], selected=selected, excluded_reason=excluded_reason)
        counts["selected_posts" if selected else "excluded_posts"] += 1
    counts["observed_posts"] = len(candidates)

    deadline = time.monotonic() + FAKE_DISCOVERY_MAX_SECONDS
    while time.monotonic() < deadline:
        if store.pipeline_stop_requested(pipeline_id):
            return {"status": "incomplete", "detail": "UI TEST FIXTURE: stop reached the fake discovery checkpoint; no feed was contacted.", **counts}
        time.sleep(0.1)
    return {"status": "incomplete", "detail": "UI TEST FIXTURE: bounded fake discovery expired without a stop request; no feed was contacted.", **counts}


def _disabled_adapter(*_args, **_kwargs):
    raise RuntimeError("This external worker is disabled in the isolated UI acceptance harness.")


def install_safe_adapters() -> None:
    # The real PipelineManager, executor, worker claims and stop/resume endpoints
    # remain active. Only external-work adapters are replaced in this process.
    pipeline_module.scan_company = fake_discovery
    pipeline_module.work_once = _disabled_adapter
    server_module.scan_company = _disabled_adapter
    server_module.work_once = _disabled_adapter
    server_module.work_batch = _disabled_adapter
    submission_module.submit_once = _disabled_adapter


def self_check() -> None:
    with tempfile.TemporaryDirectory(prefix="pipeline-ui-acceptance-check-") as folder:
        root = Path(folder).resolve()
        fixtures = seed_display_fixtures(root)
        agentic = seed_agentic_ui_fixtures(root)
        install_safe_adapters()
        app = _isolated_app(root)
        request_counter = _install_request_counter(app)
        if type(app.state.pipeline_manager).__name__ != "PipelineManager":
            raise AssertionError("The real PipelineManager must be used by the harness.")
        if not isinstance(app.state.agent_pipeline_manager, AgentPipelineManager):
            raise AssertionError("The real AgentPipelineManager must be used by the harness.")
        store: Store = app.state.store

        # Exercise the synthetic scanner with a stop already requested: it still
        # writes only .invalid listings and exits at the same checkpoint manager uses.
        active_company = _company(store, "self-check", "Self-check active company")
        active = store.create_pipeline(active_company, "Fixture Engineer", "Austin")
        store.start_pipeline(active["id"])
        store.request_pipeline_stop(active["id"])
        fake_report = fake_discovery(store, active_company, pipeline_id=active["id"])
        if fake_report["status"] != "incomplete" or fake_report["observed_posts"] != 3:
            raise AssertionError("Fake discovery did not reach the test stop checkpoint.")

        with TestClient(app) as client:
            token = client.get("/api/session").json()["token"]
            headers = {"X-Pipeline-Token": token}
            health = client.get("/api/health")
            if health.status_code != 200 or health.json().get("pipeline_release") != "agentic_pipeline_v1":
                raise AssertionError("The isolated app did not expose the current UI contract.")
            rows = client.get("/api/pipelines", headers=headers)
            if rows.status_code != 200:
                raise AssertionError(rows.text)
            records = {row["pipeline"]["id"]: row for row in rows.json()}
            if any(record["pipeline"]["company_id"] in {fixtures["active_company_a"], fixtures["active_company_b"]}
                   for record in records.values()):
                raise AssertionError("Active A/B companies must be seeded without any pipeline runs.")
            resolved = records[fixtures["resolved_pipeline"]]["pipeline"]
            resolved_counts = records[fixtures["resolved_pipeline"]]["counts"]
            if (resolved["status"] != "completed" or resolved["discovery"]["status"] != "complete"
                    or resolved_counts["submitted"] != 1 or resolved_counts["discarded"] != 1
                    or resolved_counts["selected"] != 2 or resolved_counts["excluded"] != 1):
                raise AssertionError("Resolved display fixture does not match the intended completion denominator.")
            incomplete = records[fixtures["incomplete_discovery_pipeline"]]["pipeline"]
            unresolved = records[fixtures["unresolved_pipeline"]]
            if incomplete["discovery"]["status"] == "complete" or unresolved["counts"]["submitted"] + unresolved["counts"]["discarded"] == unresolved["counts"]["selected"]:
                raise AssertionError("An intentionally incomplete display fixture became complete.")
            active_view = records[agentic["active_pipeline"]]
            if active_view["pipeline"]["mode"] != "agentic" or active_view["pipeline"]["status"] != "queued":
                raise AssertionError("The active poll card must be an agentic queued fixture only.")
            if not any(worker["role"] == "discovery" and worker["state"] == "queued"
                       for worker in active_view["workers"]):
                raise AssertionError("The active poll card is missing its saved discovery worker.")
            inbox = client.get(f"/api/pipelines/{agentic['inbox_pipeline']}/inbox", headers=headers)
            if inbox.status_code != 200 or not any(item["id"] == agentic["inbox_item"]
                                                   and item["state"] == "pending"
                                                   for item in inbox.json()["items"]):
                raise AssertionError("The agentic inbox fixture is not available through the current UI route.")
            recovery_view = records[agentic["recovery_pipeline"]]
            recovery_worker = next(worker for worker in recovery_view["workers"]
                                   if worker["id"] == agentic["recovery_worker"])
            if recovery_worker["state"] != "interrupted" or recovery_worker["resume_mode"] != "same_session":
                raise AssertionError("The saved-worker recovery fixture is not represented as expected.")
            browser = client.get("/api/browser/status", headers=headers).json()
            if browser.get("verified") or browser.get("fresh"):
                raise AssertionError("The isolated harness must not imply browser verification.")
            counts = client.get("/__ui_test/request-counts").json()["requests"]
            if counts.get("GET /api/pipelines", 0) != 1 or counts.get("GET /api/pipelines/{pipeline_id}/inbox", 0) != 1:
                raise AssertionError("The test-only API request counter did not record the expected scoped reads.")
        print("UI TEST FIXTURE self-check passed (temporary store only).")
        print("Verified current agentic pipeline, inbox, recovery, and request-counter fixtures.")
        print("No Codex runtime, model, browser, employer site, or production database was started.")


def serve_isolated() -> None:
    import uvicorn

    with tempfile.TemporaryDirectory(prefix="pipeline-ui-acceptance-") as folder:
        root = Path(folder).resolve()
        project_root = ROOT.resolve()
        if root == project_root or project_root in root.parents or root in project_root.parents:
            raise RuntimeError("Refusing a workspace that is not isolated from the real application root.")
        fixtures = seed_display_fixtures(root)
        agentic = seed_agentic_ui_fixtures(root)
        install_safe_adapters()
        app = _isolated_app(root)
        _install_request_counter(app)
        if type(app.state.pipeline_manager).__name__ != "PipelineManager":
            raise RuntimeError("The real PipelineManager is required for this harness.")
        if not isinstance(app.state.agent_pipeline_manager, AgentPipelineManager):
            raise RuntimeError("The real AgentPipelineManager is required for this harness.")
        print("UI TEST FIXTURE ONLY — isolated temporary workspace.")
        print(f"Temporary data: {root}")
        print(f"Start-test company A (no run yet): {fixtures['active_company_a']} — URL https://boards.greenhouse.io/{fixtures['active_company_a']}")
        print(f"Start-test company B (no run yet): {fixtures['active_company_b']} — URL https://boards.greenhouse.io/{fixtures['active_company_b']}")
        print(f"Resolved pipeline fixture: {fixtures['resolved_pipeline']}")
        print(f"Incomplete discovery fixture: {fixtures['incomplete_discovery_pipeline']}")
        print(f"Unresolved disposition fixture: {fixtures['unresolved_pipeline']}")
        print(f"Agentic poll fixture: {agentic['active_pipeline']} (queued discovery worker; no worker is launched).")
        print(f"Agentic inbox fixture: {agentic['inbox_pipeline']} (synthetic pending question; no answer is submitted).")
        print(f"Agentic recovery fixture: {agentic['recovery_pipeline']} (synthetic saved worker state).")
        print("The current agentic manager and API routes are real; its runtime fails closed if a worker is launched.")
        print("Legacy fake discovery creates only .invalid listings and waits for the real legacy stop flag.")
        print("Model workers, agent runtime, submission, legacy scan/batch, and fictional-practice execution are disabled.")
        print("Seeded completion cards verify display semantics only; automatic completion is covered by backend lifecycle tests.")
        print("Open http://127.0.0.1:8765; press Ctrl+C here to stop and remove the temporary workspace.")
        uvicorn.run(app, host="127.0.0.1", port=8765, log_level="warning", access_log=False)
    print("Temporary UI TEST FIXTURE workspace removed.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Isolated rendered UI acceptance harness.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--self-check", action="store_true", help="Verify fixture/routes in a disposable TestClient workspace.")
    group.add_argument("--serve", action="store_true", help="Serve disposable UI TEST FIXTURE data on 127.0.0.1:8765.")
    args = parser.parse_args()
    if args.self_check:
        self_check()
    else:
        serve_isolated()


if __name__ == "__main__":
    main()

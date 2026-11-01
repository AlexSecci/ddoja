from __future__ import annotations

from contextlib import asynccontextmanager
from collections import Counter
import hashlib
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import secrets
import subprocess
import sys
import threading
import time
from typing import Literal
import uuid

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .store import Conflict, Store, digest, timestamp
from .workers import work_once, work_batch
from .integration import browser_status
from .discovery import scan_company
from .pipelines import PipelineManager
from .pipelines import AgentPipelineManager
from .cli_paths import resolve_codex_cli
from .execution_host import ExecutionHost

ROOT = Path(__file__).resolve().parents[1]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WorkerChoice(Strict):
    provider: str = Field(max_length=40)
    model: str = Field(max_length=100)
    effort: str = Field(max_length=20)
    browser_backend: str = Field(max_length=30)
    service_tier: Literal["default", "fast"] = "default"


class RankedSubmissionBatchInput(Strict):
    count: int = Field(strict=True, ge=1, le=100)


class StageDispatchInput(Strict):
    stage: Literal["discovery", "evaluation", "submission"]
    paused: bool = Field(strict=True)


class WorkerSettingsInput(Strict):
    revision: int = Field(ge=0, strict=True)
    roles: dict[str, WorkerChoice]


class ProviderKeyInput(Strict):
    api_key: str = Field(min_length=1, max_length=4096, repr=False)


class HandoffWakeInput(Strict):
    request_id: str = Field(min_length=1,max_length=100)


class CompanyInput(Strict):
    id: str
    name: str
    careers_url: HttpUrl
    enabled: bool = False


class PipelineInput(Strict):
    company_id: str | None = None
    company_name: str | None = Field(default=None, min_length=1, max_length=200)
    careers_url: HttpUrl | None = None
    title_contains: str = Field(default="", max_length=120)
    location_contains: str = Field(default="", max_length=120)


class AgentInboxAnswer(Strict):
    response: str = Field(min_length=1, max_length=5000)
    provenance: str = Field(default="", max_length=1000)


class AgentInboxDecision(Strict):
    decision: Literal["approve", "reject"]
    provenance: str = Field(default="", max_length=1000)
    scope_token: str = Field(default="", max_length=64)


class AgentBatchItem(Strict):
    id: str = Field(min_length=1, max_length=100)
    scope_token: str = Field(pattern=r"^[a-f0-9]{64}$")


class AgentInboxBatchDecision(Strict):
    items: list[AgentBatchItem] = Field(min_length=1, max_length=100)
    decision: Literal["approve", "reject"]
    provenance: str = Field(default="", max_length=1000)


class LegacyAdoption(Strict):
    provenance: str = Field(min_length=1, max_length=1000)


class AgentProfileRefresh(Strict):
    provenance: str = Field(min_length=1, max_length=1000)

    @field_validator("provenance")
    @classmethod
    def provenance_must_be_nonblank(cls, value):
        if not value.strip():
            raise ValueError("Profile refresh requires a nonblank provenance note.")
        return value


class AgentReplacement(Strict):
    provenance: str = Field(min_length=1, max_length=1000)
    replacement_reason: str = Field(min_length=1, max_length=1000)


class EmailVerificationRecovery(Strict):
    provenance: str = Field(min_length=1, max_length=1000)
    refresh_challenge: bool = False
    resume_reserved_refresh: bool = False


class ApplicationOutcomeInput(Strict):
    status: Literal["pending", "rejected", "accepted", "unknown"]
    provenance: str = Field(min_length=1, max_length=500)


class ApprovalDecision(Strict):
    approved: bool
    provenance: str = Field(min_length=1, max_length=500)


class SubmissionAnswer(Strict):
    answer: str = Field(min_length=1, max_length=5000)
    provenance: str = Field(min_length=1, max_length=500)


class ListingFactAnswer(Strict):
    value: str = Field(min_length=1, max_length=32)
    evidence: str = Field(min_length=1, max_length=2000)
    provenance: str = Field(min_length=1, max_length=500)


class ManualAuthComplete(Strict):
    completed: Literal[True]
    provenance: str = Field(min_length=1, max_length=500)


class PipelineJobDiscard(Strict):
    reason: str = Field(min_length=1, max_length=500)
    provenance: str = Field(min_length=1, max_length=500)


class ManualJobStatus(Strict):
    status: Literal['submitted', 'discarded']
    note: str = Field(default='', max_length=500)


class ListingInput(Strict):
    company_id: str
    requisition_id: str | None = None
    url: HttpUrl
    title: str
    jd: str
    location: str
    country: Literal["US", "unknown", "other"]
    employment: Literal["full_time", "unknown", "internship", "temporary", "part_time"]
    arrangement: Literal["onsite", "hybrid", "remote", "unknown"]


class SourceInput(Strict):
    adapter: Literal["greenhouse", "lever", "lever_eu"]
    board_token: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")


class QuestionAnswer(Strict):
    answer: Literal["yes", "no"]
    context: str


class PauseInput(Strict):
    paused: bool


class PipelineVisibility(Strict):
    hidden: bool


class BatchInput(Strict):
    max_tasks: int = Field(default=20, ge=1, le=200)


class PracticeInput(Strict):
    name: Literal["Test Candidate"]
    email: Literal["candidate@example.test"]
    sponsorship: Literal["yes"]
    filename: Literal["fictional-test-resume.txt"]
    file_content: str = Field(max_length=10000)


class BrowserCheckInput(Strict):
    connection_change_note: str = Field(min_length=8, max_length=500)


def _browser_inventory_observed(inventory):
    """Accept only a normalized browser inventory tied to a successful CUA result."""
    if not isinstance(inventory, dict):
        return False
    if inventory.get("returncode") != 0 or inventory.get("timed_out") is not False:
        return False
    calls = inventory.get("mcp_calls")
    if not isinstance(calls, list) or not any(
            isinstance(call, dict) and call.get("server") == "cua_repl"
            and call.get("tool") == "js" and call.get("status") == "completed"
            for call in calls):
        return False
    if inventory.get("unapproved_action_events") != []:
        return False
    observation = inventory.get("browser_inventory_observation")
    if not isinstance(observation, dict):
        return False
    names, count = observation.get("browser_names"), observation.get("tab_count")
    if (observation.get("operation") != "cua.getState"
            or observation.get("source") != "successful_cua_repl_js_result"
            or not isinstance(names, list) or any(not isinstance(name, str) for name in names)
            or type(count) is not int or count < 0):
        return False
    normalized = json.dumps({"browser_names": sorted(set(names)), "tab_count": count}, sort_keys=True,
                            separators=(",", ":"), ensure_ascii=False)
    return observation.get("result_sha256") == hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _run_browser_check(store, app, run_id, connection_change_note, codex):
    token = None

    def finish(status, error=None):
        with app.state.browser_check_lock:
            if app.state.browser_check_state.get("run_id") == run_id:
                app.state.browser_check_state.update(status=status, finished_at=timestamp(), error=error)

    try:
        token = store.browser_lease("local-fictional-practice:" + run_id, seconds=600)
        started = time.time()
        inventory_command = [sys.executable, str(ROOT / "tools" / "integration_probe.py"),
                             "--browser", "--model", "gpt-6-luna", "--timeout", "120"]
        try:
            inventory_result = subprocess.run(inventory_command, cwd=ROOT, capture_output=True, text=True,
                                               encoding="utf-8", errors="replace", timeout=150, shell=False)
        except subprocess.TimeoutExpired:
            finish("failed", "The read-only browser inventory check timed out; no practice action ran.")
            return
        inventory_path = ROOT / "data" / "integration" / "browser.report.json"
        try:
            inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
            observed_by_cua = _browser_inventory_observed(inventory)
            fresh = inventory_path.stat().st_mtime >= started
        except (OSError, ValueError, AttributeError):
            inventory, observed_by_cua, fresh = {}, False, False
        if (inventory_result.returncode or not fresh or not observed_by_cua):
            finish("failed", "A fresh successful CUA browser inventory did not verify Chrome; no local form action ran.")
            return
        observation = inventory["browser_inventory_observation"]
        if "Chrome" not in observation["browser_names"]:
            finish("failed", "The fresh CUA inventory did not observe Chrome; no local form action ran.")
            return

        approval_path = ROOT / "data" / "integration" / "practice-approval.json"
        practice_command = [sys.executable, str(ROOT / "tools" / "browser_practice.py"), "complete",
                            "--codex", codex, "--approval-record", str(approval_path),
                            "--timeout", "300",
                            "--permission-change-note", connection_change_note]
        try:
            practice_result = subprocess.run(practice_command, cwd=ROOT, capture_output=True, text=True,
                                             encoding="utf-8", errors="replace", timeout=330, shell=False)
        except subprocess.TimeoutExpired:
            finish("failed", "The fictional local browser practice check timed out and needs a fresh status review.")
            return
        current = browser_status(store.root)
        if practice_result.returncode == 0 and current.get("verified"):
            finish("completed")
        else:
            finish("failed", current.get("detail") or "The fictional local practice check did not verify a receipt.")
    except (Conflict, OSError, ValueError, RuntimeError) as error:
        finish("failed", str(error)[:500])
    finally:
        if token:
            store.release_browser(token)


def create_app(root=ROOT, *, agent_manager_factory=None, runtime_factory=None,
               allow_http_loopback_test=False):
    store = Store(root)
    execution_host = ExecutionHost(store.root) if runtime_factory is None else None
    runtime_factory = runtime_factory or execution_host.get
    agent_manager_factory = agent_manager_factory or (
        lambda current_store: AgentPipelineManager(current_store, runtime_factory=runtime_factory)
    )

    @asynccontextmanager
    async def lifespan(application):
        # Recovery records interrupted sessions but never auto-starts any old or new run.
        application.state.agent_pipeline_manager.recover_interrupted()
        store.release_completed_auth_controls()
        try:
            yield
        finally:
            try:
                application.state.auth_broker.close()
                application.state.agent_pipeline_manager.shutdown()
            finally:
                try:
                    application.state.pipeline_manager.shutdown()
                finally:
                    try:
                        if execution_host is not None:
                            execution_host.close()
                    finally:
                        application.state.browser_check_pool.shutdown(wait=True, cancel_futures=False)

    app = FastAPI(title="Application Pipeline", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.store = store
    from .provider_credentials import ProviderCredentials
    from .worker_settings import SettingsConflict
    app.state.provider_credentials = ProviderCredentials(root)
    app.state.execution_host = execution_host
    app.state.token = secrets.token_urlsafe(32)
    app.state.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="application-worker")
    app.state.pipeline_manager = PipelineManager(store, executor=app.state.pool)
    app.state.agent_pipeline_manager = agent_manager_factory(store)
    from .auth_broker import AuthBroker
    app.state.auth_broker = AuthBroker(store, app.state.agent_pipeline_manager.schedule)
    app.state.browser_check_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="browser-practice")
    app.state.runs = {}
    app.state.browser_check_lock = threading.Lock()
    app.state.browser_check_state = {"status": "idle", "run_id": None, "started_at": None,
                                     "finished_at": None, "error": None}
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])

    @app.middleware("http")
    async def local_access(request: Request, call_next):
        origin = request.headers.get("origin")
        if origin and origin not in {"http://127.0.0.1:8765", "http://localhost:8765", "http://testserver"}:
            return JSONResponse({"detail": "Cross-origin access is not allowed."}, status_code=403)
        if request.url.path.startswith("/api/") and request.url.path not in {"/api/session", "/api/health"}:
            worker_wake = (request.method == 'POST' and request.url.path.startswith('/api/auth-handoffs/')
                           and request.url.path.endswith('/wake'))
            if not worker_wake and not secrets.compare_digest(request.headers.get("x-pipeline-token", ""), app.state.token):
                return JSONResponse({"detail": "Local dashboard session required."}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'"
        return response

    @app.exception_handler(Conflict)
    async def conflict_handler(request, error):
        return JSONResponse({"detail": str(error)}, status_code=409)

    @app.exception_handler(SettingsConflict)
    async def settings_conflict_handler(request, error):
        return JSONResponse({"detail": str(error)}, status_code=409)

    # Validation failures must not reflect a credential's input value.
    from fastapi.exceptions import RequestValidationError

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request, error):
        errors = [{k: v for k, v in item.items() if k in {"loc", "msg", "type"}} for item in error.errors()]
        return JSONResponse({"detail": errors}, status_code=422)

    @app.exception_handler(ValueError)
    async def value_handler(request, error):
        return JSONResponse({"detail": str(error)}, status_code=422)

    @app.exception_handler(KeyError)
    async def key_handler(request, error):
        return JSONResponse({"detail": "Record not found."}, status_code=404)

    @app.get("/")
    def index():
        return FileResponse(ROOT / "app" / "static" / "index.html")

    @app.get("/static/{filename}")
    def static(filename: str):
        if filename not in {"app.js", "settings.js", "style.css", "theme.css", "practice.js", "orbit-mark.svg", "space-grotesk.ttf",
                             "dm-sans.ttf", "space-grotesk-license.txt", "dm-sans-license.txt"}:
            raise HTTPException(404)
        media_type = "font/ttf" if filename.endswith(".ttf") else "image/svg+xml" if filename.endswith(".svg") else None
        return FileResponse(ROOT / "app" / "static" / filename, media_type=media_type)

    @app.get("/practice")
    def practice():
        return FileResponse(ROOT / "app" / "static" / "practice.html")

    @app.post("/api/practice/receipt")
    def practice_receipt(body: PracticeInput):
        expected = "Fictional test resume.\nTest Candidate\nC++ task scheduling integration fixture.\nNo real applicant information.\n"
        if body.file_content != expected:
            raise ValueError("Only the exact fictional fixture file is accepted by this practice endpoint.")
        receipt = {"receipt_id": uuid.uuid4().hex, "test_only": True, "name": body.name,
                   "email": body.email, "sponsorship": body.sponsorship, "filename": body.filename,
                   "file_sha256": digest(body.file_content), "received_at": timestamp()}
        folder = store.root / "data" / "practice-receipts"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{receipt['receipt_id']}.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")
        return receipt

    @app.get("/api/session")
    def session():
        return {"token": app.state.token}

    @app.get('/api/settings/gmail-broker')
    def gmail_broker_status():
        return app.state.auth_broker.status()

    @app.post('/api/auth-handoffs/{worker_id}/wake')
    def auth_handoff_wake(worker_id: str, body: HandoffWakeInput, request: Request):
        # Only the already running task's current capability may start a lookup.
        with store.connect() as db:
            row=db.execute('SELECT w.*,p.stop_requested FROM agent_workers w JOIN pipelines p ON p.id=w.pipeline_id WHERE w.id=?',(worker_id,)).fetchone()
            token=request.headers.get('x-worker-capability','')
            if (not row or row['state']!='running' or row['stop_requested'] or not row['capability_sha256']
                    or not secrets.compare_digest(digest(token),row['capability_sha256'])
                    or not row['capability_expires_at'] or row['capability_expires_at']<time.time()):
                raise HTTPException(403,'Current worker capability required.')
        from .auth_handoffs import wake
        wake(store,worker_id,body.request_id)
        return app.state.auth_broker.wake(worker_id,body.request_id)

    @app.post('/api/auth-handoffs/{worker_id}/delivered')
    def auth_handoff_delivered(worker_id: str,body: HandoffWakeInput):
        from .auth_handoffs import wake
        wake(store,worker_id,body.request_id)
        app.state.agent_pipeline_manager.schedule()
        return {'status':'checked','worker_id':worker_id}

    @app.get("/api/health")
    def health():
        browser = browser_status(store.root)
        return {"status": "ok", "release": "offline_pipeline", "pipeline_release": "agentic_pipeline_v1",
                "routine_document_uploads": {"enabled": bool(store.routine_document_uploads().get("enabled"))},
                "execution_host": (execution_host.status() if execution_host is not None else
                                   {"mode": "injected", "status": "unverified"}),
                "agent_limits": {"discovery_per_company": 1, "evaluators_per_pipeline": 3,
                                 "submissions_per_pipeline": 3, "documents_per_parent": 1},
                "pipeline_submission": "action_gated",
                "legacy_global_submission": {"available": False,
                    "note": "Global submission remains disabled; a company pipeline and current action gates are required."},
                "company_pipeline_submission": {"available": True, "mode": "agent_action_gated",
                    "note": "Agent tasks are scoped per company and job. The actual site and each requested action remain subject to browser, user-review, and evidence checkpoints."},
                "fixture_test_mode": {"allow_loopback_http_careers_url": bool(allow_http_loopback_test)},
                "browser_practice": {"verified": bool(browser.get("verified")),
                    "note": "Fictional local practice is a separate integration check; it does not grant approval for employer actions."}}

    @app.get("/api/browser/status")
    def browser_connection_status():
        with app.state.browser_check_lock:
            current_check = dict(app.state.browser_check_state)
        return {**browser_status(store.root), "check": current_check}

    @app.post("/api/browser/check")
    def run_browser_check(body: BrowserCheckInput):
        # This explicit UI action only exercises the saved approval for the local
        # fictional fixture. It never navigates to an employer site.
        if store.root != ROOT.resolve():
            raise Conflict("The local browser check is available only for the active application workspace.")
        codex = resolve_codex_cli()
        if not codex:
            raise Conflict(
                "Codex CLI was not found for this Windows user. Set JOB_PIPELINE_CODEX_PATH "
                "to its executable or update the existing installation, then restart the dashboard."
            )
        approval_path = ROOT / "data" / "integration" / "practice-approval.json"
        fixture_path = (ROOT / "tests" / "fixtures" / "fictional-test-resume.txt").resolve()
        try:
            approval = json.loads(approval_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise Conflict("The exact local fictional-practice approval record is unavailable.")
        if (approval.get("action") != "local_practice_upload_and_submit"
                or approval.get("destination") != "http://127.0.0.1:8765/practice"
                or approval.get("file") != str(fixture_path)
                or approval.get("status") != "approved"):
            raise Conflict("The saved approval does not authorize this exact local practice check.")
        with app.state.browser_check_lock:
            if app.state.browser_check_state["status"] == "running":
                raise Conflict("A local browser practice check is already running.")
            run_id = uuid.uuid4().hex
            started_at = timestamp()
            app.state.browser_check_state = {"status": "running", "run_id": run_id,
                                             "started_at": started_at, "finished_at": None, "error": None}
        try:
            app.state.browser_check_pool.submit(_run_browser_check, store, app, run_id, body.connection_change_note.strip(), codex)
        except RuntimeError:
            with app.state.browser_check_lock:
                app.state.browser_check_state.update(status="failed", finished_at=timestamp(),
                                                     error="The local browser-check runner is shutting down.")
            raise Conflict("The local browser-check runner is shutting down.")
        return {"status": "running", "run_id": run_id, "completed": False,
                "browser": browser_status(store.root)}

    @app.get("/api/summary")
    def summary():
        jobs = store.listing("jobs")
        technologies = store.listing("technologies")
        browser = browser_status(store.root)
        return {"job_phases": dict(Counter(job["phase"] for job in jobs)),
                "run_statuses": dict(Counter(job["run_status"] for job in jobs)),
                "technology_answers": dict(Counter(tech["answer"] for tech in technologies)),
                "profile_version": store.profile_version(), "paused": store.paused(),
                "legacy_global_submission": {"available": False,
                    "note": "Global submission remains disabled; use an explicitly started company pipeline."},
                "company_pipeline_submission": {"available": bool(browser["verified"]), "mode": "action_gated"},
                "browser_verified": browser["verified"], "browser": browser,
                "pending_questions": sum(q["status"] == "pending" for q in store.listing("questions")),
                "worker_runs": [{"id": key, "done": future.done()} for key, future in app.state.runs.items()]}

    @app.get("/api/efficiency")
    def efficiency(pipeline_id: str | None = None):
        return store.get_agent_efficiency(pipeline_id)

    @app.get("/api/settings/workers")
    def worker_settings():
        return store.get_worker_settings()

    @app.put("/api/settings/workers")
    def update_worker_settings(value: WorkerSettingsInput):
        return store.save_worker_settings(value.revision, {k: v.model_dump() for k, v in value.roles.items()})

    @app.put("/api/settings/providers/deepseek/key")
    def save_deepseek_key(value: ProviderKeyInput):
        try:
            return app.state.provider_credentials.save(value.api_key)
        except RuntimeError as error:
            raise HTTPException(status_code=503, detail=str(error)) from None

    @app.delete("/api/settings/providers/deepseek/key")
    def remove_deepseek_key():
        return app.state.provider_credentials.remove()

    @app.get("/api/pipelines/{pipeline_id}/runtime-status")
    def runtime_status(pipeline_id: str):
        status = store.agent_runtime_status(pipeline_id)
        status["trial"] = store.get_agent_trial(pipeline_id)
        status["execution_host"] = (execution_host.status() if execution_host is not None else
                                    {"mode": "injected", "status": "unverified",
                                     "detail": "A custom runtime factory is configured."})
        return status

    @app.get("/api/jobs")
    def jobs(state: str='all', company_id: str | None=None, order: str='score_desc', q: str=''):
        return store.job_records(state=state, company_id=company_id, order=order, query=q)

    @app.post('/api/jobs/{job_id}/manual-status')
    def manual_job_status(job_id: str, body: ManualJobStatus):
        result = store.set_manual_job_status(job_id, body.status, body.note)
        app.state.agent_pipeline_manager.schedule()
        return result

    @app.get("/api/jobs/{job_id}")
    def job_detail(job_id: str):
        return store.get_job(job_id)

    @app.get("/api/packages/{package_id}/{document}")
    def package_document(package_id: str, document: Literal["resume.pdf", "cover-letter.pdf"]):
        with store.connect() as db:
            row = db.execute("SELECT manifest FROM packages WHERE id=?", (package_id,)).fetchone()
        if not row:
            raise KeyError(package_id)
        manifest = json.loads(row[0])
        item = next((item for item in manifest.get("rendered", []) if Path(item["pdf"]).name == document), None)
        if not item:
            raise Conflict("This PDF has not completed rendering/review yet.")
        path = Path(item["pdf"]).resolve()
        folder = (store.root / "artifacts" / "packages" / package_id).resolve()
        if not path.is_relative_to(folder) or hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]:
            raise Conflict("PDF no longer matches its saved package.")
        return FileResponse(path, media_type="application/pdf", filename=document)

    @app.get("/api/companies")
    def companies():
        sources = {source["company_id"]: source for source in store.listing("company_sources")}
        return [{**company, "source": sources.get(company["id"]),
                 "discovery_supported": bool(sources.get(company["id"]) and company["enabled"] and company["id"] != "tesla"),
                 "discovery_note": "Supported Greenhouse/Lever public feed configured." if sources.get(company["id"]) else
                 "Configure a supported feed URL or wait for a company-specific discovery adapter."}
                for company in store.listing("companies")]

    @app.post("/api/companies")
    def add_company(body: CompanyInput):
        if body.id.strip().casefold() == "tesla" and body.enabled:
            raise Conflict("Tesla is excluded from the pilot.")
        company_id = store.company(body.id, body.name, str(body.careers_url), body.enabled)
        source = store.infer_source_from_url(body.careers_url)
        if source:
            store.source(company_id, source["adapter"], source["board_token"])
        return {"id": company_id}

    @app.post("/api/companies/{company_id}/source")
    def configure_source(company_id: str, body: SourceInput):
        store.source(company_id, body.adapter, body.board_token)
        return {"status": "saved"}

    def pipeline_response(pipeline):
        result = dict(pipeline)
        result.setdefault("mode", "legacy")
        result.setdefault("counts", {})
        return result

    def pipeline_view(pipeline_id):
        record = store.get_pipeline(pipeline_id)
        if record.get("mode") == "agentic":
            return app.state.agent_pipeline_manager.pipeline_view(pipeline_id)
        return {"pipeline": pipeline_response(record), "workers": [], "counts": record.get("counts", {}),
                "limits": {}, "quota": store.get_agent_quota()}

    @app.get("/api/pipelines")
    def list_pipelines():
        views = list(app.state.agent_pipeline_manager.list_views())
        included = {view.get("pipeline", {}).get("id") for view in views}
        for pipeline in store.list_pipelines():
            if pipeline.get("mode") == "agentic" or pipeline.get("id") in included:
                continue
            views.append({"pipeline": pipeline_response(pipeline), "workers": [],
                          "counts": pipeline.get("counts", {}), "limits": {},
                          "quota": store.get_agent_quota()})
        return views

    @app.post("/api/pipelines")
    def create_pipeline(body: PipelineInput):
        if body.company_name and body.careers_url:
            pipeline = store.create_agent_pipeline(
                body.company_name, str(body.careers_url),
                {"title_contains": body.title_contains, "location_contains": body.location_contains},
                company_id=body.company_id,
                allow_http_loopback_test=allow_http_loopback_test,
                test_only=allow_http_loopback_test,
            )
            return app.state.agent_pipeline_manager.start(pipeline["id"])
        if body.company_name or body.careers_url:
            raise ValueError("Company name and careers URL must be provided together.")
        if not body.company_id:
            raise ValueError("Provide a company name and careers URL to start an agentic pipeline.")
        pipeline = store.create_pipeline(body.company_id, body.title_contains, body.location_contains)
        if pipeline["status"] == "queued":
            pipeline = app.state.pipeline_manager.start(pipeline["id"])
        return pipeline_response(pipeline)

    @app.get("/api/pipelines/{pipeline_id}")
    def pipeline_detail(pipeline_id: str):
        return pipeline_view(pipeline_id)

    @app.get("/api/agent-quota")
    def agent_quota():
        return store.get_agent_quota()

    @app.post("/api/agent-quota/resume")
    def resume_agent_quota():
        result = app.state.agent_pipeline_manager.resume_quota()
        if isinstance(result, dict) and "quota" in result:
            return result
        return {"quota": store.get_agent_quota(),
                "workers": [worker for worker in store.list_agent_workers() if worker["state"] == "queued"]}

    @app.get("/api/pipelines/{pipeline_id}/workers")
    def pipeline_workers(pipeline_id: str):
        pipeline = store.get_pipeline(pipeline_id)
        if pipeline.get("mode") == "agentic":
            return {"pipeline_id": pipeline_id, "workers": store.list_agent_workers(pipeline_id),
                    "limits": app.state.agent_pipeline_manager.pipeline_view(pipeline_id).get("limits", {}),
                    "quota": store.get_agent_quota()}
        return {"pipeline_id": pipeline_id, "workers": [], "limits": {}, "quota": store.get_agent_quota()}

    @app.get("/api/pipelines/{pipeline_id}/inbox")
    def pipeline_inbox(pipeline_id: str):
        pipeline = store.get_pipeline(pipeline_id)
        if pipeline.get("mode") != "agentic":
            return {"pipeline_id": pipeline_id, "items": []}
        return {"pipeline_id": pipeline_id, "items": store.agent_inbox(pipeline_id)}

    @app.post("/api/pipelines/{pipeline_id}/inbox/{item_id}/answer")
    def answer_agent_inbox(pipeline_id: str, item_id: str, body: AgentInboxAnswer):
        pipeline = store.get_pipeline(pipeline_id)
        if pipeline.get("mode") != "agentic":
            raise Conflict("Agent inbox actions apply only to agentic pipelines.")
        item = next((candidate for candidate in store.agent_inbox(pipeline_id) if candidate["id"] == item_id), None)
        if not item:
            raise KeyError(item_id)
        answered = store.answer_agent_inbox(item_id, body.response, body.provenance)
        app.state.agent_pipeline_manager.schedule()
        view = app.state.agent_pipeline_manager.pipeline_view(pipeline_id)
        return {"item": answered, "pipeline": view}

    @app.post("/api/pipelines/{pipeline_id}/inbox/batch-decision")
    def decide_agent_inbox_batch(pipeline_id: str, body: AgentInboxBatchDecision):
        result = store.decide_agent_inbox_batch(pipeline_id, [item.model_dump() for item in body.items],
                                               body.decision, body.provenance)
        app.state.agent_pipeline_manager.schedule()
        return {**result, "pipeline": app.state.agent_pipeline_manager.pipeline_view(pipeline_id)}

    @app.post("/api/pipelines/{pipeline_id}/inbox/{item_id}/decision")
    def decide_agent_inbox(pipeline_id: str, item_id: str, body: AgentInboxDecision):
        pipeline = store.get_pipeline(pipeline_id)
        if pipeline.get("mode") != "agentic":
            raise Conflict("Agent inbox actions apply only to agentic pipelines.")
        item = next((candidate for candidate in store.agent_inbox(pipeline_id) if candidate["id"] == item_id), None)
        if not item:
            raise KeyError(item_id)
        decided = store.decide_agent_inbox(item_id, body.decision, body.provenance, body.scope_token)
        app.state.agent_pipeline_manager.schedule()
        view = app.state.agent_pipeline_manager.pipeline_view(pipeline_id)
        return {"item": decided, "pipeline": view}

    @app.post("/api/pipelines/{pipeline_id}/jobs/{job_id}/adopt")
    def adopt_legacy_job(pipeline_id: str, job_id: str, body: LegacyAdoption):
        created = store.adopt_legacy_job(pipeline_id, job_id, body.provenance)
        return app.state.agent_pipeline_manager.start(created["id"])

    @app.post("/api/pipelines/{pipeline_id}/jobs/{job_id}/refresh-profile")
    def refresh_agent_job_profile(pipeline_id: str, job_id: str, body: AgentProfileRefresh):
        pipeline = store.get_pipeline(pipeline_id)
        if pipeline.get("mode") != "agentic":
            raise Conflict("Profile refresh applies only to a selected job in an agentic pipeline.")
        return app.state.agent_pipeline_manager.refresh_job_profile(
            pipeline_id, job_id, body.provenance)

    @app.get("/api/agent-artifacts/{artifact_id}")
    def agent_artifact(artifact_id: str,
                       variant: Literal["markdown", "pdf", "preview"] | None = None,
                       index: int = Query(default=0, ge=0, le=32)):
        if variant is None:
            # Preserve the historical source-file response for clients that do
            # not specify a variant. New review links request an explicit,
            # hash-checked Markdown/PDF/preview variant below.
            artifact = store.get_agent_artifact(artifact_id)
            if artifact.get("status") != "ready":
                raise Conflict("This agent artifact has not passed its saved review checkpoint.")
            path = Path(artifact["path"]).resolve()
            media_type = artifact.get("media_type") or "application/octet-stream"
            filename = artifact.get("filename") or path.name
            expected_sha = artifact.get("sha256", "")
        else:
            artifact = store.get_agent_artifact_variant(
                artifact_id, variant, index if variant == "preview" else None)
            path = Path(artifact["path"]).resolve()
            media_type = artifact["media_type"]
            filename = artifact["filename"]
            expected_sha = artifact["sha256"]
        if not path.is_relative_to(store.root.resolve()) or not path.is_file():
            raise Conflict("The saved agent artifact is unavailable or outside the workspace.")
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if not secrets.compare_digest(actual, expected_sha):
            raise Conflict("The saved agent artifact no longer matches its recorded hash.")
        return FileResponse(path, media_type=media_type, filename=filename)

    @app.get("/api/pipelines/{pipeline_id}/jobs")
    def pipeline_jobs(pipeline_id: str, view: Literal['all','backlog']='all'):
        pipeline = store.get_pipeline(pipeline_id)
        payload = store.pipeline_jobs(pipeline_id)
        allowed = ("id", "company_id", "title", "url", "location", "country", "employment", "arrangement",
                   "phase", "run_status", "reason", "jd_version", "evaluation_status", "evaluation",
                   "application_status", "application_provenance", "selected", "excluded_reason",
                   "manual_discard_reason", "manual_discard_provenance", "manual_discard_actor",
                   "manual_discarded_at", "manual_discard", "manual_auth_resume", "agent_decision",
                   "agent_reason", "agent_status", "adoption", "submission_uncertain",
                   "application_outcome", "job_title", "profile_refresh", "fit_score", "fit_current",
                   "fit_assessment", "fit_jd_version", "fit_profile_version")
        allowed = (*allowed, 'manual_status', 'manual_status_note', 'manual_status_at',
                   'manual_submitted_available', 'manual_discard_available')
        rows = []
        for job in payload["jobs"]:
            if view=='backlog' and (job.get('phase')=='submitted' or job.get('agent_status')=='submitted' or job.get('has_submission_receipt')):
                continue
            row = {key: job.get(key) for key in allowed}
            row['has_submission_receipt']=bool(job.get('has_submission_receipt'))
            if pipeline.get("mode") == "legacy":
                row["adoption"] = store.agent_adoption_eligibility(pipeline_id, job["id"])
            rows.append(row)
        return {"pipeline_id": pipeline_id, "jobs": rows}

    @app.get('/api/pipelines/{pipeline_id}/discovery-inventory')
    def discovery_inventory(pipeline_id: str, disposition: str='all', offset: int=0, limit: int=50):
        return store.discovery_inventory(pipeline_id,disposition=disposition,offset=offset,limit=limit)

    @app.post("/api/pipelines/{pipeline_id}/stop")
    def stop_pipeline(pipeline_id: str):
        pipeline = store.get_pipeline(pipeline_id)
        if pipeline.get("mode") == "agentic":
            return app.state.agent_pipeline_manager.stop(pipeline_id)
        return pipeline_response(app.state.pipeline_manager.stop(pipeline_id))

    @app.post("/api/pipelines/{pipeline_id}/visibility")
    def pipeline_visibility(pipeline_id: str, body: PipelineVisibility):
        return {"pipeline": store.set_pipeline_visibility(pipeline_id, body.hidden)}

    @app.post("/api/pipelines/{pipeline_id}/submission-batch")
    def ranked_submission_batch(pipeline_id: str, value: RankedSubmissionBatchInput):
        policy=store.configure_ranked_submission_batch(pipeline_id,value.count)
        return {**app.state.agent_pipeline_manager.resume(pipeline_id),"submission_batch":policy}

    @app.post("/api/pipelines/{pipeline_id}/dispatch")
    def stage_dispatch(pipeline_id: str, value: StageDispatchInput):
        if not value.paused:
            return app.state.agent_pipeline_manager.start_stage(pipeline_id,value.stage)
        store.set_stage_dispatch(pipeline_id, value.stage, value.paused)
        app.state.agent_pipeline_manager.schedule()
        return app.state.agent_pipeline_manager.pipeline_view(pipeline_id)

    @app.post("/api/pipelines/{pipeline_id}/rescore-legacy")
    def rescore_legacy(pipeline_id: str):
        queued = store.queue_legacy_rescoring(pipeline_id)
        app.state.agent_pipeline_manager.schedule()
        return {**app.state.agent_pipeline_manager.pipeline_view(pipeline_id),"rescoring":queued}

    @app.post("/api/pipelines/{pipeline_id}/resume")
    def resume_pipeline(pipeline_id: str):
        pipeline = store.get_pipeline(pipeline_id)
        if pipeline.get("mode") == "agentic":
            return app.state.agent_pipeline_manager.start_campaign(pipeline_id)
        return pipeline_response(app.state.pipeline_manager.resume(pipeline_id))

    @app.post("/api/pipelines/{pipeline_id}/workers/{worker_id}/resume-replacement")
    def resume_agent_replacement(pipeline_id: str, worker_id: str, body: AgentReplacement):
        pipeline = store.get_pipeline(pipeline_id)
        if pipeline.get("mode") != "agentic":
            raise Conflict("Session replacement applies only to an agentic worker.")
        return app.state.agent_pipeline_manager.resume_replacement(
            pipeline_id, worker_id, body.provenance, body.replacement_reason)

    @app.post('/api/pipelines/{pipeline_id}/workers/{worker_id}/recover-email-verification')
    def recover_email_verification(pipeline_id: str, worker_id: str, body: EmailVerificationRecovery):
        worker = store.get_agent_worker(worker_id)
        if worker['pipeline_id'] != pipeline_id:
            raise Conflict('The worker belongs to another campaign.')
        if body.resume_reserved_refresh:
            if body.refresh_challenge:raise Conflict('Choose one recovery mode.')
            recovered=store.recover_reserved_preverification_form(worker_id,body.provenance)
        else:
            recovered = store.recover_pending_email_verification(worker_id, body.provenance, body.refresh_challenge)
        app.state.agent_pipeline_manager.schedule()
        return {'worker_id': recovered['id'], 'state': recovered['state'], 'approval_granted': False}

    @app.post("/api/pipelines/{pipeline_id}/jobs/{job_id}/manual-auth-complete")
    def manual_auth_complete(pipeline_id: str, job_id: str, body: ManualAuthComplete):
        pipeline = app.state.pipeline_manager.resume_manual_authentication(
            pipeline_id, job_id, body.provenance)
        return {"status": "acknowledged", "job_id": job_id,
                "manual_auth_resume": store.manual_auth_resume_status(job_id, pipeline_id),
                "pipeline": pipeline_response(pipeline)}

    @app.post("/api/pipelines/{pipeline_id}/jobs/{job_id}/discard")
    def discard_pipeline_job(pipeline_id: str, job_id: str, body: PipelineJobDiscard):
        discarded = store.discard_pipeline_job(job_id, pipeline_id, body.reason, body.provenance, actor="user")
        pipeline = store.get_pipeline(pipeline_id)
        if pipeline["status"] in {"blocked", "awaiting_user"}:
            pipeline = app.state.pipeline_manager.resume(pipeline_id)
        job = next(item for item in store.pipeline_jobs(pipeline_id)["jobs"] if item["id"] == job_id)
        return {"job": job, "pipeline": pipeline_response(pipeline), "discard": discarded}

    @app.get("/api/pipelines/{pipeline_id}/approvals")
    def pipeline_approvals(pipeline_id: str):
        store.get_pipeline(pipeline_id)
        return store.list_approvals(pipeline_id)

    @app.post("/api/approvals/{approval_id}/decision")
    def decide_approval(approval_id: str, body: ApprovalDecision):
        approval = store.resolve_approval(approval_id, body.approved, body.provenance, actor="user")
        pipeline = store.get_pipeline(approval["pipeline_id"])
        if body.approved and pipeline["status"] == "awaiting_user":
            pipeline = app.state.pipeline_manager.resume(approval["pipeline_id"])
        elif not body.approved:
            pipeline = store.finalize_pipeline(approval["pipeline_id"], status="blocked", stage="submission",
                                              blockers=["User declined the requested submission action."],
                                              respect_stop=True)
        return {"approval": approval, "pipeline": pipeline_response(pipeline)}

    @app.get("/api/pipelines/{pipeline_id}/questions")
    def pipeline_questions(pipeline_id: str):
        return store.list_pipeline_questions(pipeline_id)

    @app.post("/api/submission-questions/{question_id}/answer")
    def answer_submission_question(question_id: str, body: SubmissionAnswer):
        question = store.answer_submission_question(question_id, body.answer, body.provenance, actor="user")
        pipeline = store.get_pipeline(question["pipeline_id"])
        if pipeline["status"] == "awaiting_user":
            pipeline = app.state.pipeline_manager.resume(question["pipeline_id"])
        return {"question": question, "pipeline": pipeline_response(pipeline)}

    @app.post("/api/listing-fact-questions/{question_id}/answer")
    def answer_listing_fact_question(question_id: str, body: ListingFactAnswer):
        question = store.answer_listing_fact_question(question_id, body.value, body.evidence,
                                                     body.provenance, actor="user")
        pipeline = store.get_pipeline(question["pipeline_id"])
        if pipeline["status"] == "awaiting_user":
            pipeline = app.state.pipeline_manager.resume(question["pipeline_id"])
        return {"question": question, "pipeline": pipeline_response(pipeline)}

    @app.post("/api/jobs/{job_id}/application-status")
    def application_status(job_id: str, body: ApplicationOutcomeInput):
        return store.record_application_outcome(job_id, body.status, body.provenance, actor="user")

    @app.post("/api/companies/{company_id}/scan")
    def scan(company_id: str):
        if sum(not future.done() for future in app.state.runs.values()) >= 2:
            raise Conflict("Two workers are already running.")
        run_id = uuid.uuid4().hex
        app.state.runs[run_id] = app.state.pool.submit(scan_company, store, company_id)
        return {"run_id": run_id}

    @app.get("/api/scans")
    def scans():
        return store.listing("scans")

    @app.post("/api/jobs")
    def add_job(body: ListingInput):
        data = body.model_dump(mode="json")
        company_id = data.pop("company_id")
        if not any(c["id"] == company_id for c in store.listing("companies")):
            raise ValueError("Create this company source first.")
        return store.discover(company_id, data)

    @app.get("/api/technologies")
    def technologies():
        return store.listing("technologies")

    @app.get("/api/questions")
    def questions():
        return store.listing("questions")

    @app.post("/api/questions/{question_id}/answer")
    def answer(question_id: str, body: QuestionAnswer):
        store.answer_question(question_id, body.answer, body.context)
        with store.connect() as db:
            waiting = [row[0] for row in db.execute(
                "SELECT DISTINCT p.id FROM pipelines p JOIN pipeline_jobs pj ON pj.pipeline_id=p.id "
                "JOIN question_jobs qj ON qj.job_id=pj.job_id WHERE qj.question_id=? AND p.status='awaiting_user'",
                (question_id,))]
        for pipeline_id in waiting:
            app.state.pipeline_manager.resume(pipeline_id)
        return {"status": "saved"}

    @app.post("/api/pause")
    def pause(body: PauseInput):
        paused = store.paused(body.paused)
        if not paused:
            for pipeline in store.list_pipelines():
                if pipeline["status"] == "queued":
                    app.state.pipeline_manager.resume(pipeline["id"])
        return {"paused": paused}

    @app.post("/api/work/{stage}")
    def launch(stage: Literal["evaluation", "materials", "review"]):
        if store.paused():
            raise Conflict("Queue is paused.")
        if sum(not future.done() for future in app.state.runs.values()) >= 2:
            raise Conflict("Two non-browser workers are already running.")
        run_id = uuid.uuid4().hex
        app.state.runs[run_id] = app.state.pool.submit(work_once, store, stage)
        return {"run_id": run_id}

    @app.post("/api/batch")
    def launch_batch(body: BatchInput):
        if store.paused():
            raise Conflict("Queue is paused.")
        if sum(not future.done() for future in app.state.runs.values()) >= 2:
            raise Conflict("Two workers are already running.")
        run_id = uuid.uuid4().hex
        app.state.runs[run_id] = app.state.pool.submit(work_batch, store, body.max_tasks)
        return {"run_id": run_id}

    @app.get("/api/runs/{run_id}")
    def run_result(run_id: str):
        future = app.state.runs.get(run_id)
        if future is None:
            raise KeyError(run_id)
        if not future.done():
            return {"status": "running"}
        try:
            return future.result()
        except Exception as error:
            return {"status": "failed", "reason": str(error)}

    @app.get("/api/events")
    def events():
        return store.listing("events")[-100:]

    return app


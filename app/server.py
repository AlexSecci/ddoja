from __future__ import annotations

from collections import Counter
import hashlib
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import secrets
from typing import Literal
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, HttpUrl
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .store import Conflict, Store, digest, timestamp
from .workers import work_once, work_batch
from .integration import browser_status
from .discovery import scan_company

ROOT = Path(__file__).resolve().parents[1]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CompanyInput(Strict):
    id: str
    name: str
    careers_url: HttpUrl
    enabled: bool = False


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


class BatchInput(Strict):
    max_tasks: int = Field(default=20, ge=1, le=200)


class PracticeInput(Strict):
    name: Literal["Test Candidate"]
    email: Literal["candidate@example.test"]
    sponsorship: Literal["yes"]
    filename: Literal["fictional-test-resume.txt"]
    file_content: str = Field(max_length=10000)


def create_app(root=ROOT):
    store = Store(root)
    app = FastAPI(title="Application Pipeline", docs_url=None, redoc_url=None)
    app.state.store = store
    app.state.token = secrets.token_urlsafe(32)
    app.state.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="application-worker")
    app.state.runs = {}
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])

    @app.middleware("http")
    async def local_access(request: Request, call_next):
        origin = request.headers.get("origin")
        if origin and origin not in {"http://127.0.0.1:8765", "http://localhost:8765", "http://testserver"}:
            return JSONResponse({"detail": "Cross-origin access is not allowed."}, status_code=403)
        if request.url.path.startswith("/api/") and request.url.path not in {"/api/session", "/api/health"}:
            if not secrets.compare_digest(request.headers.get("x-pipeline-token", ""), app.state.token):
                return JSONResponse({"detail": "Local dashboard session required."}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'"
        return response

    @app.exception_handler(Conflict)
    async def conflict_handler(request, error):
        return JSONResponse({"detail": str(error)}, status_code=409)

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
        if filename not in {"app.js", "style.css", "practice.js"}:
            raise HTTPException(404)
        return FileResponse(ROOT / "app" / "static" / filename)

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

    @app.get("/api/health")
    def health():
        return {"status": "ok", "release": "offline_pipeline", "live_submission_enabled": False}

    @app.get("/api/summary")
    def summary():
        jobs = store.listing("jobs")
        technologies = store.listing("technologies")
        browser = browser_status(store.root)
        return {"job_phases": dict(Counter(job["phase"] for job in jobs)),
                "run_statuses": dict(Counter(job["run_status"] for job in jobs)),
                "technology_answers": dict(Counter(tech["answer"] for tech in technologies)),
                "profile_version": store.profile_version(), "paused": store.paused(),
                "live_submission_enabled": False, "browser_verified": browser["verified"], "browser": browser,
                "pending_questions": sum(q["status"] == "pending" for q in store.listing("questions")),
                "worker_runs": [{"id": key, "done": future.done()} for key, future in app.state.runs.items()]}

    @app.get("/api/jobs")
    def jobs():
        return store.listing("jobs")

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
        return [{**company, "source": sources.get(company["id"])} for company in store.listing("companies")]

    @app.post("/api/companies")
    def add_company(body: CompanyInput):
        if body.id.strip().casefold() == "tesla" and body.enabled:
            raise Conflict("Tesla is excluded from the pilot.")
        company_id = store.company(body.id, body.name, str(body.careers_url), body.enabled)
        return {"id": company_id}

    @app.post("/api/companies/{company_id}/source")
    def configure_source(company_id: str, body: SourceInput):
        store.source(company_id, body.adapter, body.board_token)
        return {"status": "saved"}

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
        return {"status": "saved"}

    @app.post("/api/pause")
    def pause(body: PauseInput):
        return {"paused": store.paused(body.paused)}

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


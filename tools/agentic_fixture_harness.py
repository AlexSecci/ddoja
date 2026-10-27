"""Serve an isolated, fictional local site for agent-pipeline acceptance.

This harness never opens the production workspace database. It copies only
agent code/configuration into a marked temporary workspace, seeds synthetic
candidate facts, opts that one app instance into loopback HTTP, and exposes a
fictional careers board plus a test-only receipt sink. Preparing or serving the
harness does not start a pipeline; the dashboard must be used explicitly.

Do not run the browser, upload a file, or click the fixture's final button until
the parent has reviewed the fixture and the user has approved that exact test.
"""
from __future__ import annotations

import argparse
from email import policy
from email.parser import BytesParser
from datetime import datetime, timezone
import hashlib
import hmac
import html
import json
from pathlib import Path
import shutil
import tempfile
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_MARKER = "AGENTIC_FIXTURE_TEST_ONLY.json"
MAX_REQUEST_BYTES = 8 * 1024 * 1024
SYNTHETIC_APPLICANT = {
    "name": "Test Candidate",
    "email": "test-candidate@example.invalid",
    "phone": "+1-202-555-0147",
    "work_authorized": "yes",
    "requires_sponsorship": "no",
}

LISTINGS = {
    "cpp-on-vehicle-data": {
        "title": "Systems Software Engineer — C++ On-Vehicle Data",
        "location": "Chicago, IL",
        "country": "US",
        "employment": "full_time",
        "arrangement": "hybrid",
        "jd": """Fictional role at Fictional Mobility Systems. This local fixture is not an employer.

The Systems Software Engineer will build C++20 services that move diagnostic data between vehicle
controllers and an on-vehicle data platform. The role requires at least four years of professional
C++ experience, Linux debugging, inter-process communication, and practical concurrency experience.
Experience with CAN messages and embedded or cross-compiled builds is preferred. A bachelor's degree
in computer engineering or equivalent experience is required. The team works hybrid in Chicago,
Illinois. This is a full-time employee role.

The fictional application form requests a resume PDF, cover-letter text, contact details, US work
authorization, and sponsorship status. No legal agreement, CAPTCHA, or real employer service is
present in this fixture.""",
    },
    "remote-platform": {
        "title": "Senior Systems Software Engineer — Remote Platform",
        "location": "United States — Remote",
        "country": "US",
        "employment": "full_time",
        "arrangement": "remote",
        "jd": "Fictional remote platform role. Remote-only arrangement is outside the fixed test requirements.",
    },
    "mechanical-integration": {
        "title": "Mechanical Integration Engineer",
        "location": "Chicago, IL",
        "country": "US",
        "employment": "full_time",
        "arrangement": "onsite",
        "jd": "Fictional mechanical integration role. It is outside the test's selected software title filter.",
    },
}


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _copy_runtime_tree(workspace: Path) -> None:
    shutil.copytree(PROJECT_ROOT / "app", workspace / "app",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "pipeline.sqlite"))
    shutil.copytree(PROJECT_ROOT / "prompts", workspace / "prompts",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (workspace / "tools").mkdir(parents=True, exist_ok=True)
    for name in ("agent_mcp.py", "render_pdf.py"):
        shutil.copy2(PROJECT_ROOT / "tools" / name, workspace / "tools" / name)
    (workspace / "config").mkdir(parents=True, exist_ok=True)
    for name in ("workers.json", "runtime.json"):
        shutil.copy2(PROJECT_ROOT / "config" / name, workspace / "config" / name)


def prepare_workspace(destination: Path | None = None) -> Path:
    if destination is None:
        destination = Path(tempfile.gettempdir()) / ("agentic-fictional-fixture-" + uuid.uuid4().hex[:12])
    workspace = destination.expanduser().resolve()
    if workspace == PROJECT_ROOT or PROJECT_ROOT in workspace.parents:
        raise ValueError("Use a separate temporary workspace; the application project cannot be the fixture DB root.")
    if workspace.exists() and any(workspace.iterdir()):
        raise ValueError("Fixture workspace must be new or empty. Existing files were left untouched.")
    workspace.mkdir(parents=True, exist_ok=True)
    _copy_runtime_tree(workspace)

    profile_path = workspace / "synthetic-applicant" / "profile.md"
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    profile_path.write_text(
        "# Fictional test candidate — no real applicant data\n\n"
        "Name: Test Candidate\n"
        "Email: test-candidate@example.invalid\n"
        "Phone: +1-202-555-0147\n"
        "Location: Chicago, Illinois\n"
        "US work authorization: Yes\n"
        "Requires employer sponsorship: No\n\n"
        "Fictional work history: Four years building C++20 Linux services for simulated vehicle-data"
        " projects, including inter-process messaging, concurrency, CAN protocol fixtures, and cross-builds.\n"
        "Fictional education: B.S. Computer Engineering, Test Institute (fictional).\n\n"
        "This profile exists only for the isolated local acceptance fixture. It describes no real person.\n",
        encoding="utf-8",
    )

    from app.store import Store

    store = Store(workspace)
    store.import_document("synthetic_test_profile", profile_path)
    for name, context in (
        ("C++", "Four years of fictional C++20 work in the isolated test profile."),
        ("Linux", "Four years of fictional Linux debugging in the isolated test profile."),
        ("Concurrency", "Fictional concurrent-service project experience for this fixture only."),
        ("Inter-process communication", "Fictional IPC project experience for this fixture only."),
        ("CAN", "Fictional CAN-message test project; not a real work history."),
    ):
        store.learn(name, "yes", context, "synthetic agentic UI fixture")

    marker = {
        "test_only": True,
        "kind": "fictional_agentic_browser_fixture",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "workspace": str(workspace),
        "source_project": str(PROJECT_ROOT),
        "applicant": SYNTHETIC_APPLICANT,
        "site_url": "http://127.0.0.1:8765/__fictional/board",
        "final_action_requires_separate_approval": True,
    }
    _write_json(workspace / FIXTURE_MARKER, marker)
    (workspace / "FIXTURE-ONLY.txt").write_text(
        "Temporary synthetic agentic UI workspace. No real applicant or employer data.\n"
        "The normal run.ps1 app does not enable loopback HTTP or fixture routes.\n"
        "No pipeline starts during prepare. Do not run the fixture browser/upload/final action\n"
        "until the fixture has been reviewed and the user has approved that exact test.\n",
        encoding="utf-8",
    )
    return workspace


def _html_page(title: str, body: str) -> str:
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{html.escape(title)}</title>"
        "<style>body{font:16px system-ui;max-width:850px;margin:2rem auto;padding:0 1rem;line-height:1.5}"
        ".fixture{background:#fff4d4;padding:.8rem;border:1px solid #d4a647}article{padding:1rem 0;border-bottom:1px solid #ddd}"
        "label{display:block;margin:.9rem 0}input,textarea,select{display:block;width:100%;max-width:36rem;padding:.55rem}"
        "button{padding:.7rem 1rem}pre{white-space:pre-wrap;overflow-wrap:anywhere}</style></head><body>"
        "<p class='fixture'><strong>FICTIONAL LOCAL TEST FIXTURE.</strong> No employer or real applicant is represented.</p>"
        f"<h1>{html.escape(title)}</h1>{body}</body></html>"
    )


def _parse_multipart(content_type: str, body: bytes) -> dict[str, tuple[str | None, bytes]]:
    if not content_type.lower().startswith("multipart/form-data;"):
        raise ValueError("The fictional form must use a multipart file upload.")
    raw = (f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("ascii") + body)
    message = BytesParser(policy=policy.default).parsebytes(raw)
    if not message.is_multipart():
        raise ValueError("The local receipt did not contain a multipart form.")
    fields: dict[str, tuple[str | None, bytes]] = {}
    for part in message.iter_parts():
        disposition = part.get("Content-Disposition", "")
        name = part.get_param("name", header="Content-Disposition")
        if not isinstance(name, str) or name in fields:
            raise ValueError("The local test form has a missing or duplicate field.")
        filename = part.get_filename()
        payload = part.get_payload(decode=True) or b""
        fields[name] = (filename, payload)
    return fields


def install_fictional_routes(app: FastAPI, workspace: Path) -> None:
    """Mount the local-only site and receipt sink on an explicitly test-mode app."""
    from app.store import Store

    workspace = workspace.resolve()
    marker_path = workspace / FIXTURE_MARKER
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError("The isolated fixture marker is missing or invalid.") from error
    if marker.get("test_only") is not True or Path(marker.get("workspace", "")).resolve() != workspace:
        raise ValueError("Fixture routes require the matching marked temporary workspace.")
    store = app.state.store
    if store.root != workspace:
        raise ValueError("Fixture site and test database must share the isolated workspace.")

    @app.get("/__fictional/board", response_class=HTMLResponse, include_in_schema=False)
    def fictional_board():
        entries = []
        for slug, listing in LISTINGS.items():
            entries.append(
                "<article><h2><a href='/__fictional/jobs/" + slug + "'>"
                + html.escape(listing["title"]) + "</a></h2><p>" + html.escape(listing["location"])
                + " · full-time · " + html.escape(listing["arrangement"]) + "</p></article>"
            )
        return _html_page("Fictional Mobility Systems Careers", "<p>Three synthetic roles for the local discovery fixture.</p>" + "".join(entries))

    @app.get("/__fictional/jobs/{slug}", response_class=HTMLResponse, include_in_schema=False)
    def fictional_job(slug: str):
        listing = LISTINGS.get(slug)
        if not listing:
            raise HTTPException(404)
        apply = (f"<p><a href='/__fictional/apply/{slug}'>Open fictional application form</a></p>"
                 if slug == "cpp-on-vehicle-data" else "")
        body = (f"<p>{html.escape(listing['location'])} · {html.escape(listing['employment'])} · "
                f"{html.escape(listing['arrangement'])}</p><h2>Job description</h2>"
                f"<pre>{html.escape(listing['jd'])}</pre>{apply}")
        return _html_page(listing["title"], body)

    @app.get("/__fictional/apply/cpp-on-vehicle-data", response_class=HTMLResponse, include_in_schema=False)
    def fictional_application():
        body = """<p>This form stores only a synthetic local receipt after the final button is clicked.</p>
<form method="post" enctype="multipart/form-data" action="/__fictional/receipt/cpp-on-vehicle-data">
<label>Applicant name<input name="name" autocomplete="off" required></label>
<label>Email<input name="email" type="email" autocomplete="off" required></label>
<label>Phone<input name="phone" autocomplete="off" required></label>
<label>Authorized to work in the United States?<select name="work_authorized" required>
<option value="">Choose</option><option value="yes">Yes</option><option value="no">No</option></select></label>
<label>Will you now or later require employer sponsorship?<select name="requires_sponsorship" required>
<option value="">Choose</option><option value="yes">Yes</option><option value="no">No</option></select></label>
<label>Resume PDF<input name="resume" type="file" accept="application/pdf,.pdf" required></label>
<label>Cover letter<textarea name="cover_letter" rows="8" required></textarea></label>
<button type="submit">Save fictional test application</button></form>"""
        return _html_page("Fictional Application — C++ On-Vehicle Data", body)

    @app.post("/__fictional/receipt/cpp-on-vehicle-data", response_class=HTMLResponse,
              include_in_schema=False)
    async def fictional_receipt(request: Request):
        body = await request.body()
        if len(body) > MAX_REQUEST_BYTES:
            raise HTTPException(413, "The synthetic test file is too large.")
        try:
            fields = _parse_multipart(request.headers.get("content-type", ""), body)
            text = {name: content.decode("utf-8", errors="strict")
                    for name, (filename, content) in fields.items() if filename is None}
            filename, resume = fields.get("resume", (None, b""))
        except (ValueError, UnicodeError) as error:
            raise HTTPException(400, str(error)) from error
        if any(text.get(key, "").strip() != value for key, value in SYNTHETIC_APPLICANT.items()):
            raise HTTPException(422, "Only the fixed fictional Test Candidate facts are accepted.")
        cover_letter = text.get("cover_letter", "").strip()
        if not cover_letter or len(cover_letter) > 20000:
            raise HTTPException(422, "A synthetic cover-letter response is required.")
        if filename != "resume.pdf" or not resume.startswith(b"%PDF-"):
            raise HTTPException(422, "Upload the reviewed fictional resume.pdf artifact.")
        resume_hash = hashlib.sha256(resume).hexdigest()
        with store.connect() as db:
            job = db.execute("SELECT j.id,p.id AS pipeline_id FROM jobs j JOIN pipeline_jobs pj ON pj.job_id=j.id "
                             "JOIN pipelines p ON p.id=pj.pipeline_id WHERE j.url=? AND p.agent_test_only=1 "
                             "ORDER BY p.created_at DESC LIMIT 1",
                             ("http://127.0.0.1:8765/__fictional/jobs/cpp-on-vehicle-data",)).fetchone()
            artifact_row = db.execute(
                "SELECT a.id FROM agent_artifacts a JOIN jobs j ON j.id=a.job_id "
                "WHERE a.job_id=? AND a.kind='resume' AND a.status='ready' AND a.jd_version=j.jd_version "
                "AND a.profile_version=? ORDER BY a.version DESC,a.created_at DESC LIMIT 1",
                (job["id"], store.profile_version()) if job else (None, None),
            ).fetchone()
        if not job or not artifact_row:
            raise HTTPException(409, "This fixture job has no current ready resume artifact.")
        # The ready Store row binds the authored Markdown to the PDF produced by
        # the real renderer. Verify both files through Store and compare the
        # upload against the renderer's PDF hash, never the Markdown source hash.
        artifact = store.get_agent_artifact(artifact_row["id"])
        upload_path = Path(artifact.get("upload_path") or "").resolve()
        source_path = Path(artifact["path"]).resolve()
        package_root = source_path.parents[1]
        if (artifact.get("filename") != "resume.md" or upload_path.name != "resume.pdf"
                or upload_path.parent.name != "rendered" or upload_path.parent.parent != package_root
                or package_root.parent.name != "agent"
                or not hmac.compare_digest(resume_hash, artifact.get("pdf_sha256", ""))):
            raise HTTPException(409, "The upload does not match this fixture job's current rendered resume PDF.")
        receipt = {
            "test_only": True,
            "fixture": "fictional_agentic_browser_fixture",
            "receipt_id": uuid.uuid4().hex,
            "company": "Fictional Mobility Systems",
            "pipeline_id": job["pipeline_id"],
            "job_id": job["id"],
            "applicant": dict(SYNTHETIC_APPLICANT),
            "filename": filename,
            "resume_sha256": resume_hash,
            "reviewed_artifact_id": artifact["id"],
            "resume_source_sha256": artifact["sha256"],
            "resume_package": package_root.name,
            "cover_letter_present": True,
            "cover_letter_sha256": hashlib.sha256(cover_letter.encode("utf-8")).hexdigest(),
            "received_at": datetime.now(timezone.utc).isoformat(),
        }
        receipts = workspace / "data" / "fictional-test-receipts"
        receipts.mkdir(parents=True, exist_ok=True)
        _write_json(receipts / (receipt["receipt_id"] + ".json"), receipt)
        return _html_page("Fictional test receipt saved", "<p>Receipt: <strong>" + html.escape(receipt["receipt_id"])
                           + "</strong></p><pre>" + html.escape(json.dumps(receipt, indent=2)) + "</pre>")


def serve(workspace: Path, host: str = "127.0.0.1", port: int = 8765) -> None:
    workspace = workspace.expanduser().resolve()
    marker = json.loads((workspace / FIXTURE_MARKER).read_text(encoding="utf-8"))
    if marker.get("test_only") is not True or Path(marker.get("workspace", "")).resolve() != workspace:
        raise ValueError("Not a verified fixture-only workspace.")
    if host != "127.0.0.1" or port != 8765:
        raise ValueError("The fictional fixture is fixed to 127.0.0.1:8765 for the separately approved local test.")
    from app.server import create_app
    import uvicorn

    application = create_app(workspace, allow_http_loopback_test=True)
    install_fictional_routes(application, workspace)
    uvicorn.run(application, host=host, port=port, log_level="info")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare", help="Create a new empty synthetic test workspace; does not start agents.")
    prepare.add_argument("--workspace", type=Path, help="Optional new or empty workspace path outside the project.")
    run = sub.add_parser("serve", help="Serve the isolated UI and fictional site on loopback only.")
    run.add_argument("--workspace", type=Path, required=True)
    run.add_argument("--host", default="127.0.0.1")
    run.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if args.command == "prepare":
        workspace = prepare_workspace(args.workspace)
        print(json.dumps({"workspace": str(workspace), "test_only": True,
                          "site_url": f"http://127.0.0.1:8765/__fictional/board",
                          "server_started": False, "pipeline_started": False}, indent=2))
        return 0
    serve(args.workspace, args.host, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

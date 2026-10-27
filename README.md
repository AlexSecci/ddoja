# Application Pipeline

Persistent local application tracking and company-by-company job pipelines. The agentic workflow uses the existing Codex/ChatGPT subscription; it does not configure a separate API-key inference service.

## Current status — 2026-10-23

**UI update, October 24:** the dashboard now uses the dark After Hours design with local fonts, persistent navigation, an orbital overview, clearer stage/status cues, paginated records and searchable technology cards. Editing/polling safeguards remain. The suite passes 220 tests; browser acceptance covers all five views at five screen sizes. See the [design and browser acceptance notes](docs/ui-design.md).

The efficiency rebuild is installed and the dashboard is running with application queues stopped. It adds compact role contexts and versioned updates, exact browser-JD capture, shared factual questions, safe evaluation/document reuse, company sign-in ownership, shared browser-failure recovery, measured usage and stable dashboard editing. The same saved contexts are 67–72% smaller for evaluation/submission/writing; this is a character measurement, not a subscription savings estimate. The final isolated suite passes 219 tests. All original values across 33 tables match the pre-rebuild backup after activation. See the [complete requirement audit and measured limits](docs/efficiency-implementation.md) and [architecture decisions](docs/efficiency-architecture.md).

The three core roles and 1/3/3 caps remain. A writer child still starts only after an actual supported document field is observed. New writer tasks share a persistent subscription app-server process with isolated threads; browser workers and existing CLI sessions retain their original transport. Failed turns never silently switch transports. Browser support in app-server is not yet verified and is disabled there.

The application acceptance and earlier suite counts below are historical. This rebuild did not resume a company or submit an application.

The current pipeline starts with a company name and careers URL. Discovery agents inspect a company's careers site; evaluator agents assess one job at a time; submission agents handle the job's application; and a separate Sol document child is started only when the observed form calls for materials. Agents decide what listings, requirements, questions, and fields mean. The host enforces task scope, persisted checkpoints, current document hashes, safe resume rules, and human action approvals rather than scripted skill or quote checks.

- Fixed run scope is computer-science-related work in the United States, full-time, and on-site or hybrid. The optional role focus narrows this search; leaving it blank includes varied CS titles and duties such as software/developer, systems/platform, GPU/HPC, backend/frontend/full-stack, data, and automation. No exact “Software Engineer” title is required. Location is also optional. A company run has one discovery worker and up to three evaluators and three submission workers; a document child is outside that limit.
- Questions pause only the affected job. First-application review and action-specific approvals are separate, human decisions. The review shows the current rendered PDFs, Markdown sources, form fields and observed values/evidence before a first final action.
- A pipeline completes only when discovery reports full coverage and every selected listing has a verified submitted result or a discard. Excluded listings are outside that denominator. Prepared or ready materials do not count as submitted. Uncertain final actions remain unresolved.
- Recruiter outcomes are entered manually with a source and note. Automatic employer-status polling is not available.
- The isolated loopback HTTP exception exists only in the test fixture app and allows only the exact URL `http://127.0.0.1:8765/__fictional/board`. Normal app instances require HTTPS careers URLs.

The normal dashboard is active with 89 historical Tesla submissions and 100 confirmed technology records (84 Yes, 16 No). Tesla is disabled. The legacy Zoox job and unanswered questions are preserved; its supervised agentic re-evaluation is tracked separately. The actual production activation preserved all original values across 26 tables while adding agentic metadata: [activation preservation report](data/demos/20261017T095651Z-post-activation-preservation/activation-preservation.json). A fresh consistent backup is retained under `data/backups/`.

The first supervised live pilot also completed: Zoox **Software Engineer – C++ On-Vehicle Data**, Foster City, CA, with the exact title filter selected by the user. After the user's final review approval, Zoox displayed **Application Received!**, employer application ID `example-receipt-id`. The dashboard recorded one verified submission, completed the run automatically and left recruiter outcome pending. The worker's completed tab was closed and its absence independently verified. This is one live application, not a full unfiltered Zoox sweep. See [the current requirement audit](data/demos/20261017T112512Z-zoox-live-acceptance/final-requirement-audit.md).

The real-agent fictional workflow passed end to end: dashboard start, browser discovery of three full descriptions, one evaluator approval, an on-demand Sol document child, reviewed PDFs, exact approved upload, receipt-backed submission, automatic completion and application-tab cleanup. The same submission session survived an interruption and missing-tab recovery. Evidence and earlier failures are retained in [the acceptance archive](data/demos/20261017T094449Z-agentic-fictional-acceptance/final-requirement-audit.md). This is synthetic local evidence, not an employer submission.

Rendered checks also passed for [manual recruiter-status changes](data/integration/20261017T095008Z-agentic-fictional-manual-outcome-ui.json) and [stopping A while B continues](data/integration/20261017-agentic-controls-stop-isolation-ui.json). The control test uses the real manager and a test-only waiting runtime, with no application-agent calls. New verified submissions start as pending recruiter response when no status exists; manual and historical outcomes are preserved. Automatic recruiter polling is unavailable, so record employer responses through the outcome form with their source.

The earlier full offline suite passed 152 tests. A later combined run had 166 passes and one incomplete UI fixture failure; its fixture correction passed the isolated rerun. Subsequent provenance/session checks passed two tests and the final rationale-label check passed one. Focused profile/manager/UI checks, source compilation, JavaScript syntax and whitespace checks passed; no new full-suite pass is claimed. One existing upstream deprecation warning remains. The fixture-only URL validation, asynchronous start-form reset, scoped internal-tool approvals and documented file-chooser procedure were corrected during the real browser trial. The normal service requires HTTPS careers URLs; no fictional applicant data was imported into it.

## Start the local dashboard

Double-click **Job Application Pipeline** on the Windows desktop to start the dashboard in the background and open it in your browser. It reuses an already running service. To recreate that shortcut, run `.\install-desktop-shortcut.ps1`; the launcher and startup logs are documented in [the user guide](docs/user-guide.md#start-and-stop-the-local-service).

From PowerShell in this project directory:

```powershell
.\run.ps1
```

Before starting the worker-capable service, the launcher resolves the Codex CLI and checks only its version and existing login status; it does not start an agent task or make a model request. To run this check without starting the server, use `.\run.ps1 -PreflightOnly`. If the CLI is missing, set `JOB_PIPELINE_CODEX_PATH` to the installed executable or repair the official desktop CLI. If it is signed out, the preflight prints a quoted PowerShell login command for the resolved executable. Run it in the same Windows user session to use your existing ChatGPT account, then verify with the printed status command; no separate API key is required. `.\run.ps1 -Check` retains its offline test-only behavior and does not require CLI sign-in.

Routine tailored résumé/cover-letter uploads can use the user's recorded standing authorization for approved jobs on the selected employer's application sites. Current file hashes and recipient scope accompany the worker's prepared materials; routine uploads do not generate another inbox approval. Other files/destinations and binding/CAPTCHA checkpoints retain their own requirements. See [the inbox and upload policy](docs/user-guide.md#answer-questions-and-review-checkpoints).

On Windows, each Codex worker or probe process receives its own `TEMP`/`TMP` folder under `data/agent-runtime/`. This lets the read-only sandbox access the browser-control support files while keeping the override limited to that process; global PATH and sandbox settings are unchanged.

Open `http://127.0.0.1:8765`. The normal service binds to this computer. After a reboot or service restart, refresh the existing dashboard tab to obtain a current local session, then explicitly choose **Resume this company** for the saved pipeline you want to continue. Interrupted work is not launched automatically; do not create a duplicate pipeline to recover it. Stop the foreground server with Ctrl+C in its owning terminal. Do not rely on the historical `data/server-process.json` record to identify a running service.

The checked-in `.venv` can be recreated with an installed Python 3.11+ runtime:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## Isolated fictional fixture

The fixture creates a new temporary workspace with synthetic candidate facts, a local three-listing board, and a receipt sink that validates the current rendered resume PDF hash. Prepare and serve it as a Python module from the project root:

```powershell
python -m tools.agentic_fixture_harness prepare
python -m tools.agentic_fixture_harness serve --workspace <path-printed-by-prepare>
```

Serving does not start a pipeline. In the dashboard, use Company name `Fictional Mobility Systems`, Careers page URL `http://127.0.0.1:8765/__fictional/board`, Role title `Software Engineer`, and leave Location blank. This observes three fixture listings; fixed requirements and title narrowing select one. The test fixture's application contains synthetic values only. See [the fixture guide](docs/agentic-fixture-acceptance.md) for its checkpoints and limits.

## Checks

The [26 October token-efficiency report](docs/token-efficiency-20261026.md) records measured model counters, batching and cache changes, queue backpressure, and the rejected aggressive-compaction experiment. Application campaigns remain stopped until explicitly resumed.

```powershell
.\.venv\Scripts\python.exe -m pytest -q
node --check app/static/app.js
```

## Historical tools and evidence

The earlier feed-first workflow, public Greenhouse/Lever adapters, and scripted evaluation/material/review workers remain available for historical records and bounded diagnostics. They are not the current agentic decision path; do not treat their fixed semantic gates or demo outcomes as current application decisions. The previous Greenhouse public-feed capture and older scripted demos are documented in [integration-status.md](integration-status.md).

The canonical fictional browser-practice run completed on 2026-10-15 in about 193 seconds. The practice server verified the synthetic name, email, sponsorship choice, fixture filename, and uploaded file hash. The browser's email readback was masked; the local receipt is authoritative. This result proves only that fixed fictional form flow, not an employer submission.

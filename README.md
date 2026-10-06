# Application Pipeline

Persistent local pipeline for the applicant, using the existing Codex/ChatGPT subscription. No API key or separately billed API inference route is enabled.

## Current release

- SQLite job queue, description versions, applicant source snapshots, technology answers, shared questions, atomic task claims, recovery and evidence ledger.
- 89 prior Tesla applications imported from the saved account snapshot; 56 individual confirmation receipts retained separately. Submission attribution is unknown where it was not established. Tesla is disabled as a pilot source.
- 100 confirmed knowledge records imported: 84 Yes and 16 No, with context and provenance. Historical pending questions remain in the imported source document; they have not been treated as No.
- Real subscription tests completed for GPT-6 Luna and GPT-6.1 Sol.
- Read-only Greenhouse and Lever/Lever EU feed adapters save raw pages, descriptions and complete/incomplete/failed scan coverage. No live employer has been scanned by this release. Missing jobs are not automatically closed.
- Evaluation, material and review workers use GPT-6.1 Sol high through supported `codex exec` structured output, with read-only filesystem access, API-key fallback disabled and browser plugins disabled for that invocation. Explicit location/employment mismatches are discarded without a model call.
- PDF generation uses the bundled document runtime, text/page checks and PNG previews. Independent source and visual review gates package readiness. Rejected drafts can be revised; after three failed versions, that job waits for manual review while others can continue.
- Dashboard controls cover company assignment, scanning, questions, offline batches, job explanations and PDF downloads. On-demand batches advance evaluation, drafting and review; no recurring automation is installed. The visible task limit bounds one run, not the total number of applications.
- All 38 checks pass. A fictional subscription run resolved missing listing metadata, drafted materials, caught an internal workflow note, revised the draft and reached ready for submission. A separate deliberate unsupported 99% latency claim was rejected. These small checks do not establish a live-employer success rate.

Chrome inventory is verified in this chat and a launched Luna worker. The local browser practice test is blocked by a saved site permission for `127.0.0.1`; navigation, filling and upload are **not yet verified**. Renewed user approval and Full Access did not clear that result. Current user screenshots show Default Browsing set to Always allow with no custom site rows visible; a fresh direct worker check still returned the saved block. No supported user/project browser configuration override was found. The cause remains unresolved; see `browser-permission-diagnostic.md`. Worker-owned tab handoff, interactive approvals, submission receipts and post-confirmation tab closure remain to be verified and connected. **Live submission is disabled**, even for a reviewed package. This release is not a complete application autopilot.

The existing background server still runs the older release. A restart was rejected as “blocked by policy.” Updated controls stay disabled until the service is restarted. `restart.ps1` is a manually runnable helper that validates the saved process identity before stopping the dashboard. It is syntax checked and has not been executed. A visual check of the updated dashboard remains pending restart and browser access.

## Start

From PowerShell in this directory:

```powershell
.\run.ps1
```

Open `http://127.0.0.1:8765`. The server binds only to the local computer. Close the terminal or press Ctrl+C to stop a foreground run. If an agent starts it in the background, its process record is saved in `data/server-process.json`.

For an already running background instance, review and manually run `restart.ps1`, then refresh the browser. Do not start another instance on the same port.

The `.venv` environment is already installed. To recreate it using an available Python 3.11+ installation:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## Checks and worker commands

```powershell
.\run.ps1 -Check
.\.venv\Scripts\python.exe tools\worker.py evaluation
.\.venv\Scripts\python.exe tools\worker.py materials
.\.venv\Scripts\python.exe tools\worker.py review
.\.venv\Scripts\python.exe tools\batch.py --max-tasks 20
.\.venv\Scripts\python.exe tools\local_worker_demo.py
.\.venv\Scripts\python.exe tools\negative_review_demo.py
```

The demo uses fictional applicant/company data in a separate directory under `data/demos/`; it does not modify the real application ledger. Model calls consume the existing subscription's usage allowance. Usage reports record tokens and elapsed time; they are not dollar cost calculations.

An evaluation can approve/discard/ask. Unknown mandatory technologies produce a shared question; user answers persist with context and affected jobs are reassessed. Non-technology eligibility questions and public listing facts that need further research are currently visible through job reasons; richer question routing is a remaining integration step.

Material workers generate drafts. Rendering checks text and page counts; a separate model reviews factual support and every rendered page. Passed packages are frozen with PDF hashes and become ready for submission. An automated review can still miss an error; inspect the first live pilot before submitting. `config/runtime.json` points to the verified bundled document runtime, Poppler, Node and fonts on this computer.

Source documents are imported from the previous task's canonical Markdown files. The original files are unchanged. Updated source imports preserve document snapshots and invalidate affected prepared work. Existing submissions remain protected against rediscovery and changed descriptions.

## Next implementation steps

1. Resolve the saved local-site permission, then verify local form filling/upload, same-session pause/resume, receipt capture and cleanup. Worker inventory is already verified.
2. Activate the updated service with the reviewed manual restart helper, then visually check the new company/batch/PDF controls.
3. Finish general eligibility-question routing and public listing research gaps; technology questions already have the full answer/requeue flow.
4. Integrate browser submission checkpoints, applicable action-time approvals, receipts/account reconciliation and tab closure.
5. Select a non-Tesla live pilot with the user after the local browser test passes. Browser-based company adapters and recurring runs can follow.

See [implementation-plan.md](implementation-plan.md) and [integration-status.md](integration-status.md).

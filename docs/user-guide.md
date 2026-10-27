# Using the local pipeline dashboard

Run `.\run.ps1` from the project directory and open `http://127.0.0.1:8765`. The dashboard and its records are stored locally in this workspace.

## Start and monitor a company

On **Pipelines**, enter a company name and an HTTPS careers-page URL. You can use a regular company careers site; a Greenhouse or Lever feed is not required. Every new pipeline searches computer-science-related work in the **United States**, **full-time**, and **on-site or hybrid**. The optional **Role focus** narrows that search; leave it blank to include CS-related jobs across varied titles and duties, such as software/developer, systems/platform, GPU/HPC, backend/frontend/full-stack, data, or software-automation work. An exact “Software Engineer” title is not required. You can also add an optional location phrase. Agents inspect the actual site and interpret role duties; the dashboard does not use a scripted title or technology keyword gate.

You can run multiple companies independently. Each company has one discovery worker, up to three evaluators, and up to three submission workers. A requested document child can run outside the submission-worker limit. The card shows the current stage, worker role/state, saved session identifier, resume mode, checkpoint, counts, and recruiter outcomes. **Stop this company** affects that pipeline; **Resume this company** continues its saved work when available. A quota pause is account-wide and requires the separate **Resume after quota reset** control after you confirm the allowance has reset.

Counts separate all discovered listings from those selected for evaluation and those excluded by a run's filters. A pipeline is complete only after the discovery worker reports complete coverage and every selected listing has a recorded submitted result with evidence or a discard. Excluded listings are shown separately and do not enter that denominator. A document package or a ready-to-submit form is not a submission. Pending questions, approvals, quota pauses, failures, and uncertain final actions are not terminal completion. Recruiter outcomes are tracked separately and do not change whether the application was submitted.

Expand **Worker sessions** to inspect each role and saved checkpoint. The dashboard prefers the original Codex session. A replacement-session form appears only when the runtime reports the session as definitively unavailable; it records a reason and provenance. It is not a retry control for a possibly sent application.

Worker lists show queued/running work and workers needing attention. Completed, discarded, superseded and stopped workers are tucked into **Finished worker history**; expand it when you need the audit trail. A finished-looking worker with a turn still active remains visible until its transport exits. Failed, interrupted, quota-paused, uncertain and waiting workers remain visible.

Completed and stopped company cards have **Hide campaign**. This preference survives refreshes and restarts, without changing applications, outcomes or worker history. Use **Show hidden campaigns** and **Restore campaign** to bring one back. Active campaigns must be stopped before hiding. Explicitly resuming a stopped campaign restores its visibility.

Company resume also recovers workers left quota-paused after the account pause was cleared while that company was stopped. Pending questions/approvals remain waiting, and possibly sent applications remain reconciliation-only. A failed/inactive sign-in owner can hand the sign-in mutex to another eligible worker; the gate stays unverified until that new owner observes the actual account. Each explicit resume provides one fresh tab-recovery attempt at the current canonical job/careers URL, while retaining the saved agent session and original history.

Routine sign-in can use the known application email and Chrome's saved autofill. When you explicitly authorize email verification for a company, its single sign-in owner can retrieve only the just-requested code from the named Gmail account and enter it directly into that company's observed challenge. The worker uses a dedicated mail tab (or an available scoped mail connector), does not print/save the code or mail body, and closes its temporary mail tab. Authentication-phase tool payloads are replaced by event metadata and hashes in the pipeline database. This authorization does not approve new account/credential changes, CAPTCHA, terms, or unrelated mailbox access. Missing/ambiguous messages, inaccessible mail accounts and unavailable authentication factors still require a concrete handoff.

Fresh browser workers read the installed Computer Use skill through `get_browser_guidance` in their scoped bridge, which permits only the skill and its three bundled guidance files. General shell/file tools remain outside worker scope. Resuming an unfinished document task restores its parent's waiting state before the child can run, within the three-parent submission limit.

## Answer questions and review checkpoints

Open **Agent inbox** for unavoidable job-specific blockers, the first-application review, and action approvals. Agents assess credible stretch applications using the resume, saved answers and transferable experience; a missing JD skill, exact duration or experience-depth detail does not trigger a skills survey. They retrieve relevant sources first, use supported deductions (for example, the documented education/employment timeline establishes over-18 status), and record unverified qualifications without claiming them. Optional unknown claims/fields can be omitted. A genuinely unknown mandatory form answer or necessary personal decision can still need your input.

Each inbox item shows the role, job, agent context, and available reviewed document links. Enter your answer and send it; your reply is automatically recorded as a direct applicant answer. There is no separate source field. Reusable applicant facts are shared with their scope; answering does not approve an application action.

First-application review and action approvals have separate **Approve** and **Reject** choices. Read the exact item context and choose; a note is optional and the selected action is recorded automatically. An approval applies to its specific job checkpoint, not to other jobs or later actions. The UI does not treat a free-form “yes” as approval.

The user's standing upload instruction covers current ready tailored résumés and cover letters for evaluator-approved jobs at the companies they manually select. These routine uploads do not need a separate inbox approval. Workers receive the exact current PDF path/hash and employer/application-site scope, verify the actual form and recipient, and record the upload checkpoint/result. Changed/stale material, unrelated sites, other file types, explicit rejections and browser denials are outside that permission. Binding agreements, CAPTCHA and other actual action-time requirements remain separate. The launch form shows whether this standing upload permission is enabled; it is stored as operating policy rather than as an invented per-file approval or applicant fact.

The **Legacy questions** section and **Saved companies** view keep prior records readable. Old runs do not start automatically and their machine-generated answers are not treated as user answers. For an eligible unsent legacy job, choose **Adopt and re-evaluate** and provide provenance. This creates a separate agentic run while preserving the original pipeline and question history.

## Historical records and recruiter outcomes

**Applications** shows saved job records from the previous workflow. Open a record to inspect its stored evidence and available historical package files. For a verified submitted application, record **Pending recruiter**, **Rejected**, **Accepted**, or **Unknown**, plus the source and a short note. These outcomes are manual records with provenance; the dashboard does not claim automatic recruiter-status polling.

**Technology history** is the saved applicant knowledge record. It remains separate from the agents' interpretation of a job description.

## Browser practice status

The **Browser practice check** is a separate, explicit test of the fixed fictional local form. It does not visit an employer, prove access to employer sites, or authorize an application action. The canonical fictional run passed on 2026-10-15; see `data/integration/practice-submit-20261015T153810Z.report.json`. Its receipt was validated by the local practice server. Zoox and other employer forms remain outside that test.

## Start and stop the local service

Double-click **Job Application Pipeline** on the Windows desktop. It starts the dashboard in the background and opens it in your default browser once ready. If the service is already running, it opens that instance. Repeated clicks do not start duplicate servers. Saved companies still require an explicit **Resume this company** from the dashboard.

The shortcut runs `launch-dashboard.ps1`; CLI/login checks and server logs are saved in `data/launcher/`. Startup errors appear in a message box. To recreate the shortcut, run `.\install-desktop-shortcut.ps1`. For background startup without opening a browser or showing message boxes, run `.\launch-dashboard.ps1 -NoBrowser`.

For a foreground server with visible logs:

```powershell
.\run.ps1
```

The launcher checks the resolved Codex CLI version and existing login status; it does not run models during preflight. To check readiness without starting the server, run `.\run.ps1 -PreflightOnly`. The resolver checks the configured CLI path, `JOB_PIPELINE_CODEX_PATH`, PATH, and the normal Windows Codex install location. If the CLI is signed out, preflight prints a quoted PowerShell login command for the resolved executable. Run it in the same Windows user session to use your existing ChatGPT account, then verify with the printed status command; no separate API key is required. `.\run.ps1 -Check` runs offline tests only and skips CLI preflight.

On Windows, each Codex worker or probe gets its own `TEMP`/`TMP` folder under `data/agent-runtime/`, where the read-only sandbox can access browser-control support files. The override applies only to that child process; it does not change global PATH or sandbox settings.

Open `http://127.0.0.1:8765`. After a reboot or server restart, refresh the existing dashboard tab to obtain a current local session, then explicitly choose **Resume this company** for the saved pipeline you want to continue. Startup records interrupted work but does not launch it automatically; do not create a duplicate pipeline. Stop the foreground server with Ctrl+C in the terminal that owns it. The saved `data/server-process.json` record is historical and should not be used to identify the current server.

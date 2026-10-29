# Agent-driven pipeline: accepted implementation contract

Updated 2026-10-29. This supersedes the earlier feed-first architecture and scripted semantic gates in implementation-plan.md. The user explicitly specified this workflow and answered the operating questions in this chat.

## Decision authority

Agents interpret websites, job descriptions, applicant facts, requirements, alternatives, forms and confirmation pages. Code must not reverse an agent's eligibility decision by looking up technology strings, applying inferred sponsorship/location rules, requiring exact quoted excerpts, or matching a success-phrase lexicon. Structured tool arguments/results are a transport format, not a second decision-maker.

Code owns authentication/scope, durable records, company/requisition-or-URL deduplication, scheduling, concurrency, stop state, snapshot/version integrity, file paths/hashes, action approval records and links to actual tool transcripts. Structural validation is appropriate. The agent's interpretation and supporting observations remain visible for review. Do not invent evidence or treat an agent's intended tool arguments as a successful tool result.

## Current fixed career scope (user clarification, 2026-10-17)

All runs pursue CS-related jobs only, including with a blank role focus. Discovery and evaluation judge actual responsibilities across varied titles, not a keyword whitelist or exact title. Optional title_contains remains the compatible transport field for a narrower semantic role focus; blank adds no narrowing. New filter snapshots persist career_scope, and both launch/get_context supply the current mandatory policy even for older snapshots without rewriting history. Discovery receives this safe career context without applicant documents/contact details. Clearly unrelated work is not selected; plausible CS-related ambiguity goes to evaluation. Career-change flexibility stays within this scope.

## Accepted workflow

1. The user starts a company pipeline with a company, careers URL and filters in the dashboard. One configured discovery agent explores that site using the supported browser. It records individual listings incrementally through dashboard tools, with the full JD, actual requisition/application ID when published, exact URL, observed metadata and its selection decision. The database rejects duplicates; it does not invent IDs. If no ID is published, use the established URL identity fallback and record that limitation. The agent determines when the site's listings have been exhausted and reports coverage/completion. No company-specific feed adapter is required. Existing adapters may remain historical utilities but are not the new pipeline's mandatory path.
2. Each selected new listing immediately queues a configured evaluator for that one JD, while discovery continues. It reads the master resume, profile and answered questions, repairs clearly missing/incomplete JD information by opening the actual URL when needed, and publishes a 0–100 fit score with material requirements, evidence, gaps, confidence and confirmed hard blockers. A perfect or overqualified fit scores 100. Near-year experience gaps and transferable skills are credited; low fit is not an eligibility rejection. A confirmed non-negotiable conflict scores 0 and is blocked; all positive eligible scores remain in the ranked backlog; 0 is not submitted. It distinguishes required/preferred/alternative skills and depth of experience. It asks only genuinely missing facts through the dashboard. One logical evaluator handles exactly one job and completes when that evaluation is finished.
3. A separate ranked-backlog consumer queues configured submission agents in descending fit order as slots become available. It opens and understands the actual form. When the form supports a resume, cover letter, or both, it requests one dedicated document child using the configured materials model for the supported artifacts. No documents are generated speculatively for every approved job.
4. The document child receives the exact JD, canonical applicant facts, confirmed answers and existing document templates. It creates truthful tailored documents, inspects the generated output, publishes the files and completes. The parent continues in its same saved session/tab. One child may prepare both documents for that request. A cover-letter text field also counts as a supported cover-letter field; do not require a file upload control to recognize it.
5. The submission agent fills the form and handles normal permitted actions. Unknown facts, required action-time approvals and the first live application review are dashboard checkpoints. Only that application waits. After actual confirmation, the agent records the outcome with its supporting browser observations/transcript, receives a durable acknowledgment and closes only its own completed tab.

## Concurrency, waiting and recovery

- Discovery is capped at one active agent per company, including when more than one run exists. Per pipeline: up to 3 active evaluators and up to 3 active submission parents. Document children do not consume submission slots; at most one active document child per parent. Parents waiting for their child retain their submission slot. Human/question/quota-paused tasks release active execution slots so unrelated jobs can progress.
- No global one-browser lease may silently serialize all pipelines. Each browser task owns its own session/tab. A necessary connector limitation must be surfaced explicitly rather than reported as three-way execution.
- Questions pause the affected job, not discovery or the entire pipeline. Shared answered knowledge is supplied to future agents. Agents decide whether a new question duplicates an answered fact; code may deduplicate an explicit question key chosen by the agent.
- Quota errors are persisted as quota_paused with the actual provider error, session, job, role and checkpoint. An account-wide quota latch stops new launches to avoid repeated failures against the shared subscription. In-flight tasks may reach safe checkpoints. Manual Resume after quota reset clears the latch and resumes eligible tasks within the caps; explicitly stopped pipelines remain stopped.
- Resume prefers the same persistent Codex session and logical task. A replacement session is allowed only after the original is definitively unavailable; record the reason and supply durable context. Never replay a possibly sent final application. Such a task is uncertain and reconciliation-only, including after restart or quota failure.
- Long discovery must not be killed by the old 240-second whole-task timeout. Persist sessions and incremental progress during execution. A timeout/interruption is a checkpointed interruption, not an automatic fresh task or failed application.
- The first actual application remains held for the user's review before final submission. CAPTCHA and binding-agreement approvals still occur at their actual browser-policy checkpoints; broad pipeline authorization does not fabricate them.
- Natural completion requires the discovery agent to report complete coverage and every selected job to have a terminal submitted/discarded result. Questions, quota pauses, errors and uncertain sends are not terminal completion. Recruiter outcomes remain separate, with a stated manual path when polling is unsupported.

## Shared implementation interfaces

These names are the coordination contract for the three implementation workers. Changes require a message to the other workers before editing their call sites.

Backend owns additive schema, Store and AgentPipelineManager. Keep legacy data readable; do not start legacy semantic runners from the updated UI.

- Store.create_agent_pipeline(company_name, careers_url, filters) -> pipeline
- Store.list_agent_workers(pipeline_id=None) -> workers
- Store.get_agent_worker(worker_id) -> worker
- Store.agent_inbox(pipeline_id) -> inbox items
- Store.answer_agent_inbox(item_id, response, provenance) -> item
- Store.agent_action(worker_id, token, action, payload) -> tool result
- Store.agent_runtime_event(worker_id, event) -> durable transport event update
- Store.agent_launch_context(worker_id) -> scoped task/context and short-lived tool capability
- Store.finish_agent_turn(worker_id, result) -> transport disposition, preserving any tool-committed checkpoint/outcome
- AgentPipelineManager(store, runtime_factory=None): start, stop, resume, shutdown, recover_interrupted, resume_quota

The backend may add internal helpers. Pipeline API responses retain existing card counts and add mode=agentic, workers, limits, quota and clear active/waiting details. Worker records include id, parent_id, pipeline_id, job_id, role, state, session_id, resume_mode, checkpoint, context, error, updated_at. No credentials/tool capability appear in UI responses. Session IDs are identifiers, not credentials.

The runtime worker owns app/agent_runtime.py (CodexAgentRuntime), tools/agent_mcp.py, role prompts and document integration helpers. Its run(task, on_event, should_stop) returns a transport result and streams real session/tool/usage/error events. It launches the scoped MCP bridge with worker identity/capability supplied by the backend, using per-run supported configuration, never editing global authentication. Test that bridge in a harmless real CLI session before relying on it. Browser roles receive supported CUA; evaluators may use CUA for JD repair. Document workers receive only scoped document tools/local previews.

The MCP bridge delegates operations to Store.agent_action, with scope bound by the launch capability rather than a model-supplied company/job ID. Required operations: get_context, lookup_listing, record_listing, finish_discovery, update_job_description, ask_question, finish_evaluation, checkpoint, request_documents, publish_documents, request_approval, finish_submission. Browser roles need checkpoint and applicable action-approval tools even during discovery/JD repair; submission and document agents need the question tool if required facts are missing. A submission parent's document request enqueues a real dedicated Sol child and returns its ID promptly; the parent hands off its tab and waits. The scheduler resumes that exact parent after child completion. Do not block the MCP call for minutes waiting for a child or recursively use a capped parent executor for children.

Agents determine which form fields exist, which documents are useful, what a JD means and whether a page confirms submission. The host must not issue a document capability by parsing HTML for English field names or require literal receipt excerpts as a semantic acceptance test. It can enforce action role/scope, exact approved files/origin/snapshot and persistence of genuine successful tool-result evidence. Save unstructured observations with transcript references for agent/user review.

UI worker owns app/server.py, static files, UI/API tests and user docs. Existing POST /api/pipelines becomes the agentic start entry accepting company_name, careers_url, title_contains and location_contains (optional saved company_id can remain compatible). Routes include pipeline workers/inbox, answering an inbox item, existing stop/resume, and manual account-quota resume. The dashboard exposes the 1/3/3 limits, individual worker/session/checkpoint, question/document/review waits, quota state, preserved-session versus replacement recovery and unresolved submission outcomes. An arbitrary HTTPS careers URL must not be blocked for lacking a Greenhouse/Lever adapter.

## Data preservation and migration

Before implementation, the parent stopped the old foreground service and made a consistent SQLite backup: data/backups/pipeline-before-agent-rebuild-20261015T162611Z.sqlite. Preserve the 89 historical Tesla submissions, 100 knowledge records, existing versions, questions and audit evidence. Never relabel a generated answer as a user answer.

The existing Zoox pipeline eb77cd941efc45a7ab0f894ad4588e44 is paused at redundant legacy questions, selected job c614ae4cc4e144e4a489e71cdbdb6b6f. No employer application ran. Provide an explicit adoption/re-evaluation path that preserves the identity/history and supersedes the legacy system-generated questions with system provenance, without answering them or inheriting an old semantic approval. Do not automatically activate any migrated real pipeline during tests or server startup.

## Required acceptance evidence

1. Real harmless CLI/MCP handshake plus saved-session continuation through the new scoped bridge; no API key route.
2. Isolated streaming test: selected listings are evaluated while discovery remains active; positive decisions start submission without waiting for full scan. Separate agent sessions/tasks per JD.
3. Scheduler tests demonstrate 1/3/3 caps, independent pipelines, child execution outside parent slots and job-local question waits.
4. Known C++20/concurrency facts do not trigger a scripted unknown gate. A genuine unknown is asked through the dashboard and its answer resumes the same evaluator session. Agents, not string heuristics, decide fit.
5. Arbitrary-site discovery, JD repair and actual browser form handling tested through a fictional local board/application; document child is started only on a form-supported request and produces reviewed outputs.
6. Quota/interruption recovery retains task/session/checkpoint; manual resume is idempotent and uncertain final sends are never blindly retried. Stop affects one pipeline and no new action starts after its safe stop boundary.
7. Rendered UI shows start, workers, inbox, quotas/resume, counts, stop and first-application review. Real ledger is preserved. Then resume the approved supervised Zoox pilot through the corrected path.

Do not claim the rebuilt pipeline complete based only on legacy test counts, mock agent responses or successful CLI launch. Keep local fixture proof, live-employer proof and remaining limitations distinct.

## Official integration references checked on 2026-10-15

- https://learn.chatgpt.com/docs/cli/reference documents persistent exec resume by explicit session ID and JSONL events.
- https://learn.chatgpt.com/docs/app-server documents thread/resume and typed UsageLimitExceeded errors. The CLI path is already proven locally; do not introduce a second inference/billing framework without need.

## Ranked fit backlog (2026-10-29)

Evaluation and submission have independent durable boundaries. `finish_evaluation` records a versioned assessment and does not create a scored submission worker. The scheduler consumes current eligible assessments separately, under the existing three-active-submission cap. It takes the highest score currently available without waiting for discovery or other evaluations. Newly discovered stronger roles do not preempt tasks already started; saved in-progress application continuations finish before fresh work. Ties use stable queue timestamps and IDs. Every positive eligible score is retained, with no additional cutoff; 0 is not submitted. The score is relative applicant fit, never a claim about interview/offer probability.

The dashboard lists jobs highest-fit first and exposes grouped requirement matches, source evidence, confidence, rationale and hard barriers. Its separate **Pause new evaluations** / **Pause new submissions** controls preserve the other stage, existing sessions and the backlog. Already-started tasks and their required document children may continue to finish. Enabling a stage does not resume a stopped campaign or clear quota. Submission delays, pending approvals and connector outages do not throttle offline evaluations; discovery can still buffer behind a full evaluation queue.

The scoring rubric is agent guidance in `prompts/evaluation_agent.md`; `app/fit_scoring.py` performs structural validation, not semantic requirement classification or weighted keyword matching. Eligibility means no confirmed hard barrier, not full qualification. Material unknowns affect confidence and fit without becoming invented negative facts. Hard blockers require job and applicant evidence. Preserve all actual-action approvals and uncertain-send/receipt guards.

Scores and their JD/profile versions are saved in pipeline membership and evaluator results/events; exact-score cache entries include the scoring-policy version. JD/profile changes make scores stale and prevent fresh dispatch. Older binary assessments and their pinned worker contracts remain historical/unscored (no fabricated numeric backfill); fresh evaluations use the scored tool contract. Legacy cache entries cannot satisfy a scored evaluation. Completed submissions and earlier uncertain sends are never automatically re-applied to obtain a score.


The **Re-score older assessments** action queues fresh evaluator workers for safe unscored legacy judgments (including earlier fit-only rejections). It supersedes unsent unfinished work while keeping completed verdicts and results in history. It excludes receipts, submitted/uncertain/terminal application results, possible sends, manual discards, active work and pending inbox actions. It is idempotent and does not resume a stopped campaign. An evaluation-only bounded run may fill this scored backlog with submissions paused; its admission closes when the selected jobs have current scores.

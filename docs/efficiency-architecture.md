# Architecture decision: independent tasks, shared infrastructure

October 23, 2026. This supplements the accepted agentic contract. Agents still decide fit, meaning, truthful tailoring and employer outcomes. Code does not become an eligibility judge.

## Keep three core roles and an on-demand writer

The default remains company discovery → one-JD evaluation → job submission, with a writer child created only for fields observed on the application. Keep the existing 1/3/3 concurrency caps and one child per submission parent. Do not create a model-based dispatcher, polling agent, universal reviewer, or separate worker for each field.

| Alternative | Assessment |
| --- | --- |
| Merge discovery and evaluation | The browser scan becomes coupled to individual questions and long applicant context. It loses incremental evaluation while discovery continues and the one-JD task boundary. |
| Merge evaluation and submission | Every judgment task needs browser/account state; factual questions and recovery become coupled to potentially consequential actions. It also requires choosing one model/effort for two quite different workloads. |
| Merge evaluation and writing | Writing before observing the form can create unused documents or the wrong artifact format. Keep evaluator findings as a compact handoff so the later writer uses them without repeating the eligibility decision. |
| A separate permanent document stage | Adds a queue and speculative work. A parent-requested child preserves document quality and produces only requested material. |
| Add an orchestrator/reviewer agent | The scheduler already dispatches on durable events. An extra model call per transition adds context and latency without solving a demonstrated gap. Structural guards and focused quality checks remain host responsibilities; substantive ambiguity returns to reasoning or the user. |
| Share a persistent execution host | Useful infrastructure optimization if connector capability and thread isolation are verified. It must not merge logical workers or carry one application's approvals/account/tab into another. |

The retained boundaries serve different lifetimes and authority: discovery owns site coverage; evaluation owns one fit decision; submission owns a form and its uncertain-send ledger; writing owns a requested artifact. A factual question pauses dependent work, company login gates only protected account work, and a connector outage prevents repeated failures. Public discovery and completed JD evaluation can continue independently.

## Make handoffs small and durable

The database holds full source records and audit transcripts. Agents receive a complete current JD, a compact index that exposes all skills and source sections, and versioned retrieval handles. They retrieve evidence when relevant, including adjacent skills and depth. A submission worker receives the evaluator's findings and unresolved caveats, confirmed form facts and artifact handles. It rechecks changes rather than redoing the whole decision.

Browser observations are selected by an agent and persisted by the host. Passing a capture reference avoids generating the JD again as tool arguments. Batch operations handle identities and independent fields from a fresh observation; drift or a consequential boundary returns control to reasoning. Reusable ATS plans are agent-authored and verified against the current form, not handwritten company adapters or scripted screening rules.

Evaluation reuse requires unchanged JD, candidate profile, preferences and policy. Render reuse requires identical content, renderer and style inputs, with hash checks. A cached decision never carries an action approval or turns an uncertain send into permission to retry. Authentication and one-time codes are operational state, not reusable applicant facts.

## Execution and effort

Keep Sol high for evaluation/writing and current Luna settings while measuring reduced-context behavior. The first optimization is less repeated information and transport work, not weaker reasoning. Change effort only after the same saved descriptions and fictional forms retain sound decisions, required questions, accurate facts, complete discovery and correct receipts.

The default is now a hybrid host. New document tasks use one service-owned subscription app-server process with independent scoped threads. Browser tasks retain the supported CLI/CUA transport. Every existing saved CLI session also stays on its original transport, including document sessions. A qualified `app_server:` session can resume only through that host; a missing registry or host failure never causes an automatic replacement or replay on another transport.

Two harmless actual document turns verified the bridge, actual PDF preview images, same-thread continuation and host restart/thread resume. Protocol fixtures verify concurrent thread/capability isolation and no fallback after failure. Browser/CUA capability in app-server remains unverified and is deliberately disabled there. The document timings include different rendering/cache work, so they do not isolate process-startup savings. No API key, credential extraction or second billing route is introduced. The implementation follows the installed protocol schema and [official app-server documentation](https://learn.chatgpt.com/docs/app-server).

## Evidence and practical limits

The pre-change isolated suite passed 192 tests; the final suite passes 219. Historical context replies have a median of 228,426.5 characters. The original proposal's 294 completed CUA transport events include 45 failed calls; the read-only baseline separates successful observations from failures. These historical runs contain debugging and human waits and do not prove a clean end-to-end speedup. Character counts are transport measurements, not tokens, credits or subscription allowance.

Against the saved snapshot, evaluator initial context falls from 229,031 to 64,829 characters, submission from 245,592 to 80,546, and writing from 232,429 to 69,727. Routine controls are 1,212–1,217 characters. Complete JDs and all three original sources remain exactly retrievable. Actual saved-JD evaluation approves the C++ systems/data role with sponsorship explicitly unknown and rejects the mechanical closures role on its duties. This two-case check is regression evidence, not a statistical quality guarantee or a new production verifier.

Keep Luna medium for discovery, Luna high for submission, and Sol high for evaluation/writing. On six identical fictional observation cases, medium matched four expected action labels and high matched six; the medium explanations still preserved studied-versus-production and uncertain-send distinctions. This sample is too small and unlike live browsing to justify a blanket effort reduction. Savings currently come from avoiding repeated work and data, not weakening evaluation or adding concurrency.

The requirement matrix and acceptance results live in `efficiency-implementation.md`. Application queues remain stopped throughout this rebuild.

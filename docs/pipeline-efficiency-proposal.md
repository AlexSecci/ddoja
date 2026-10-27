# Pipeline efficiency proposal

Prepared October 22, 2026. Proposal only; application work is stopped. No optimization below has been implemented by this review.

## What the current implementation and saved logs show

- The saved events contain 98 completed `agent_bridge.get_context` replies. Median text payload: 228,426 characters; maximum: 267,738. These are character counts, not tokenizer measurements.
- The latest sampled evaluator, submission, and document contexts each contain an applicant section of 221,743 serialized characters. `_agent_profile_context` sends all source documents, technology records, aliases, and answered questions.
- Source documents contain 110,537 characters of master resume, 36,229 of application profile, and 26,782 of technology history. Some of this information is repeated in structured records and question histories.
- The initial runtime prompt already carries scoped context, then the role prompts require a full `get_context` call. Worker state, prepared materials, and histories can also occur in multiple sections of the same context.
- Recorded browser activity contains 294 completed CUA calls. Text replies have a median of 8,772 characters and a maximum of 104,142. This includes setup, normal work, and failure/recovery attempts; it is not a clean successful-application benchmark.
- The bridge exposes separate identity lookup and listing-record operations: 74 lookup results and 45 record results are present. The host already owns deduplication.
- Existing role-specific tool allowlists and event-driven scheduling are useful and should be retained.
- Resumed-session usage counters need normalization before summing: all 19 multi-turn session groups have monotonically increasing input/output counters. Confirm the CLI's counter semantics and use deltas where counters are cumulative. Cached input is part of input, not an additional input total. These logs do not establish an exact subscription-allowance saving.

## Decision boundary

Agents continue to interpret unstructured JDs, duties, requirements, alternatives, candidate evidence, form questions, and employer confirmations. No title/technology keyword filter or scripted eligibility verifier replaces those judgments. Deterministic helpers handle transport, identity deduplication, scoped execution of agent-approved actions, persistence, rendering, and workflow integrity.

## Token improvements, in priority order

1. **Small, role-specific contexts with source retrieval.** Provide the full current JD, compact candidate fact index, relevant evidence, and live controls. Keep every source available through scoped retrieval. Discovery needs filters/cursors/identity sets; evaluation needs experience and knowledge; submission needs confirmed form facts and artifact handles; document writing needs relevant achievements and style. The index must expose adjacent skills and project summaries so retrieval does not hide transferable experience. Preserve dates, negative answers, unknowns, proficiency depth, and provenance.
2. **One initial context, then deltas.** Make launch data a small task envelope and let one context call retrieve the necessary bundle, or hydrate once at launch and let the first tool return live controls plus changed data. Never append another full 228k-character bundle merely to check quota, an answer, or a completed child. Keep audits in storage rather than injecting their entire history into every turn.
3. **Stable instructions plus phase-specific guidance.** Keep core truthfulness, scope, permission, stop, and uncertain-send rules always present. Load upload instructions, rendering details, or recovery procedures when relevant. Do not remove documentation required by the active browser tools. Reuse stable prefixes, but do not assume API cache configuration is exposed through our subscription-backed Codex runtime.
4. **Avoid model-mediated copying.** Add a browser-supported capture/handoff operation that persists the exact agent-selected rendered JD and returns its ID/hash. The evaluator still reads the complete JD. Likewise pass artifact handles between stages instead of making each agent reproduce a resume or cover letter. This is data transport, not scripted interpretation.
5. **Bound long discovery history.** Keep the scan frontier, visited identities, and coverage in the DB. Return the active cursor and bounded progress, not the complete growing scan history. Use measured compaction within the same logical task when useful; do not reset at every page, since that can lose cache reuse and important context.
6. **Share confirmed facts and unresolved questions.** A confirmed answer becomes a dated, scoped fact with evidence. When agents identify the same missing fact, one question can serve the dependent jobs. Agents must establish semantic equivalence; Spark is not automatically Flink, and studied knowledge is not production experience. Operational sign-in questions remain separate from candidate facts.
7. **Reuse unchanged work.** Cache evaluations against complete JD, candidate-profile, preference, and policy versions. Cache rendered documents against exact content and style hashes. Maintain verified base resumes for broad role families, with an agent choosing and tailoring material for each role. Do not reuse a decision after its relevant inputs change.
8. **Tune effort after context reduction.** Keep Sol for evaluation and writing initially. Benchmark medium versus high effort on routine browser execution; reserve deeper reasoning for unfamiliar forms or ambiguity. Avoid a second model call just to decide which model should handle every tiny operation.

## Speed improvements without adding agents

1. **One company authentication gate.** Authenticate once in the intended browser profile. Protected submission work waits behind that login; public discovery/evaluation continues. Avoid simultaneous sign-in/code requests across three tabs. Each application still verifies its current account and form, and retains its own legal/action approvals.
2. **Batch known browser actions.** After observing a page, let an agent prepare a bounded plan for independent fields, execute that group, and read the resulting state once. Inspect new/changed questions individually. Stop batching before submit, a new permission boundary, or an ambiguous action.
3. **Use compact browser observations.** Return the relevant JD or form region, visible questions, required state, validation errors, and changed values. Store full snapshots for audit; retrieve them when needed. Screenshots and PDF previews remain available for visual issues and required review without being repeatedly injected into unrelated turns.
4. **Batch identity operations and reuse verified workflow structure.** Check one result page's identities in one host call. Atomically record/deduplicate captures. Agents can save reusable ATS navigation/form plans after observing them; later agents verify the present structure and delegate changed parts back to reasoning. This avoids handwritten adapters for every company.
5. **Remove redundant handoff work.** Carry evaluator findings and their uncertainties into submission so the parent does not redo eligibility analysis. Preserve the existing on-demand document child. Cache template/layout work; use deterministic PDF rendering while the writer reviews actual content/layout changes.
6. **Keep the execution host alive where supported.** Prototype a persistent subscription-backed Codex host across turns to reduce repeated process startup, tool initialization, and browser rebinding. Keep one logical evaluator per JD and current caps. Measure actual savings and verify tool/session isolation; do not assume CUA state or approvals survive a host change.
7. **Stop shared failures early.** A shared browser outage or authentication problem should open one circuit breaker, rather than send multiple agents into the same failure. Keep job-specific problems local. Resume on a real state change, with bounded safe read retries; never replay a potentially sent application.

## Quality and measurement gate

Use saved varied JDs and fictional form flows first, with no employer submission. Compare the existing and proposed pipeline on the same inputs. Track uncached/cached input, output/reasoning, model/tool calls, machine time, human-wait time, retries, and successful stage completions separately. Separate development/debugging runs from ordinary processing.

Review whether role decisions remain sound, transferable skills stay discoverable, missing required facts are asked, facts are truthful, documents remain well tailored and readable, discovery coverage is maintained, and forms/receipts are handled correctly. Offline evaluation is a development check, not a new production eligibility verifier. Reject an optimization that saves usage by losing necessary evidence or skipping a decision.

Recommended implementation order: measurement/accounting and context slimming; browser observations/action batching and company authentication; reuse/caching and persistent-host prototype; calibrated effort changes last. Do not promise a total speedup or allowance percentage before measuring this workload.

## References

- OpenAI latency optimization: https://developers.openai.com/api/docs/guides/latency-optimization
- OpenAI prompt caching, including the compaction/cache-reuse tradeoff: https://developers.openai.com/api/docs/guides/prompt-caching
- Codex subscription usage considerations: https://learn.chatgpt.com/docs/pricing?codex-pricing-plans=individual

API documentation supplies general design principles here, not a proposal to switch billing or assume API-only controls are available in Codex.

# Five independent pipeline improvements — measured results

Implemented and deployed on 28 October 2026. Implementation commits: `0dbc75b`, `59e51cb` and `7dbe573`; baseline: `22847b0`. All original data across 44 production tables remains identical, SQLite quick check is `ok`, and all campaigns remain stopped. The existing per-role provider/model/effort settings are unchanged. The full suite passes **335 tests**, with one existing Starlette/httpx deprecation warning.

The five areas have separate switches in `config/efficiency.json`. Each measurement changes one area or one interface path, keeping its other fixture conditions fixed. Browser/scheduler tests use actual Chromium and synthetic local forms. Token tests use the existing DeepSeek key through the same Codex CLI with synthetic text and an optional read-only ping. No employer applications, mailbox access, new approvals or production workers were used.

| Area | Independent observed result |
|---|---|
| Browser lifetime | Selected résumé upload calls **2 → 1** across a disconnected/restarted worker. Unsaved values, selected file and transient JS state survived CLI termination and actual owner-host process exit. Another worker's profile remained isolated. |
| Grouped field edits | The same three mapped fields plus current observation required **2 → 1 model-facing calls**. All actual form values matched. Returned payload was **1,217 → 1,139 characters**; browser time stayed approximately 46–47 ms. |
| Context/cache | Controlled DeepSeek input **57,235 → 27,715 tokens**, a **51.6% reduction**. Uncached input **11,923 → 7,747**, down **35.0%**. Stable source/policy prefixes and complete-source recovery were verified. |
| Document regeneration | Both exact historical drafts that exceeded two pages now fit **two pages with unchanged source text**, using readable fixed 10pt layouts. A controlled localized edit used **943 → 93 model output tokens**, down **90.1%**, producing identical document lines. |
| Browser serialization | The same two existing local form-preparation workers completed in **4.719 → 2.344 seconds**, down **50.3%**. Peak simultaneous preparation changed 1 → 2 within the same two-worker pool; role caps and shared authentication still apply. |

These are independent mechanism/fixture measurements, not a claim that production campaign throughput improves by the same percentages. They must not be added together. Model samples are small, caching and provider/network conditions affect latency, and scheduler tests include a fixed 200ms local page delay.

## 1. Preserve browser lifetime

`app/browser_sessions.py` starts an owned loopback Playwright MCP HTTP service from the dashboard-side runtime, outside the worker CLI process tree. Each worker has a separate service, profile, port and creation identity. CLI stdio adapters reconnect through `tools/browser_mcp.py`. The service disables idle browser shutdown so long approvals/quota waits do not silently destroy unsaved forms.

Live-process creation identity is checked before reuse/cleanup, protecting against PID reuse. A live but unreachable service raises a recovery condition instead of being replaced. A genuinely lost service gets a new generation and is reported as lost continuity. If a final send may have happened, loss does **not** automatically launch a replacement browser. Completed/discarded tasks receive owner-only cleanup after their CLI turn exits; stopped, quota-paused, waiting and uncertain tasks keep their browser. Cleanup failures retain an attention event without changing a saved receipt.

Resume instructions now inspect the actual retained page first instead of unconditionally reloading it. Account/job freshness and possible-send rules remain enforced. A PC reboot cannot preserve in-memory form state, and the employer can still expire authentication; the worker must inspect and recover the real draft in those cases.

Evidence: `browser-lifetime.json`; tests for process identity, no replacement after timeout/PID reuse/possible send, and existing resume/recovery regressions.

## 2. Reduce model round trips

The enhanced `browser_fill_form` applies an agent-mapped group and returns a genuine fresh inline snapshot in the same model-facing response. Generated fill code is not redundantly echoed. Agents are instructed to reuse this result rather than immediately asking for the same snapshot. Optional exact-URL checks stop before editing a changed page; an observed form/section can scope the resulting snapshot.

This convenience path batches ordinary text/select/slider fields. Consent/choice controls, signatures, uploads, CAPTCHA and final sends stay separate. Partial errors return current state and never automatically retry. Discovery still captures complete JDs. Agents retain field mapping and semantic decisions.

The fixture deliberately uses the already-efficient native bulk fill as its control: one fill plus one observation, compared with the enhanced single response. It does not assume the old agent typed every field individually. The extra internal snapshot still takes browser time; the eliminated cost is a model/tool round trip, not eliminated UI observation.

Evidence: `field-batching.json`; tests for exact values, changed URL, binding-control exclusion and partial-error/no-retry behavior.

## 3. Reduce context while preserving cache reuse

Playwright workers use runtime-specific core/role instructions rather than a full native-CUA workflow plus an overriding adapter. Unchanged adapters are not appended on every resume. For workers without native CUA, unrelated app connector integration is disabled per CLI invocation, leaving supervisor-owned Gmail retrieval intact. Native CUA remains available when selected.

Repeated source requests in one invocation revalidate scope/snapshot, then return a small unchanged reference. Complete original content is restored with `retrieve_sources(force=true)` if compaction lost it. No source documents or complete JDs are truncated. Native compaction defaults remain unchanged; this does not force the previously rejected aggressive compaction policy or erase existing conversation history.

Structural measurements on identical inputs:

- Launch envelope: 48,915 → 14,483 bytes, down 70.4%.
- Resume envelope: 11,378 → 1,314 bytes, down 88.5%.
- Three repeated source results: 99,228 → 33,444 bytes, down 66.3%.
- Stable-prefix equality across different worker/job IDs: passed in both cases.

Bytes are not tokens. The actual separate DeepSeek/CLI control measured:

| Variant | Total input | Cached input | Uncached input | Output | Elapsed |
|---|---:|---:|---:|---:|---:|
| Full context | 57,235 | 45,312 | 11,923 | 139 | 6.750 s |
| Compact context | 27,715 | 19,968 | 7,747 | 157 | 4.719 s |

Both performed the same one read-only ping and structured completion, with no browser/dashboard work. This isolates context/catalog overhead, not application decision quality. The smaller prefix was newly introduced, so cache-hit fractions are not assumed equal; absolute uncached input decreased as well.

Evidence: `context-structural.json`, `context-False.json`, `context-True.json`, and source-restoration/scope/stable-prefix regressions.

## 4. Reduce résumé rewrites

Inspection of the original trial showed both first DeepSeek drafts failed because they rendered as three pages. The renderer now tries the existing standard layout and, on overflow, a fixed readable compact layout with the same 10pt body size, margins and all authored text. It does not drop facts or shrink text below that bound. If content still exceeds the limit, the agent must shorten it. The layout mode is part of the render-cache fingerprint.

For the exact two failed drafts, standard rendering failed in about 0.25 seconds; the successful two-layout path took about 0.69 seconds per document. Both outputs passed text verification and all four final page images were visually inspected for legibility, spacing, overlap and clipping.

The new `edit_documents` accepts exact grouped replacements against an own draft or indexed verified résumé base and its source hash. It preserves unchanged text, creates an immutable new version and returns actual PDF previews through the same renderer. Wrong hashes, ambiguous targets and cross-worker draft access fail before publishing a new logical version. Writers are explicitly told not to republish just to acknowledge an acceptable preview.

Rendered PDFs and previews now use directories addressed by source content and renderer fingerprint. Later edits cannot overwrite an earlier version's PDF or invalidate its cache entry. Existing artifacts keep their legacy paths and hash verification. A real renderer regression verifies old bytes remain unchanged, the edited preview differs, and publishing the original source again reuses its verified cached PDF.

The controlled model edit comparison used one small summary edit on an existing synthetic document:

| Encoding | Input | Cached | Uncached | Output | Output bytes | Elapsed |
|---|---:|---:|---:|---:|---:|---:|
| Entire revised document | 7,035 | 4,736 | 2,299 | 943 | 4,120 | 5.031 s |
| Exact replacement | 7,042 | 6,016 | 1,026 | 93 | 98 | 2.672 s |

Applying the replacement and taking the full rewrite produced identical document lines. Only a terminating newline is ignored; other whitespace/content is not normalized away. This measures localized edit encoding, not a promised 90% saving when writing a new résumé from scratch. Rendered PDF review remains mandatory in the worker instructions and available for both publish/edit tool results.

Evidence: `document-layout/results.json`, `document-layout/visual-review.json`, `document-edit-False.json`, `document-edit-True.json`, immutable-source/scope tests and renderer integration tests.

## 5. Scope serialization to shared state

The existing company form-writer setting stays at one for shared native sessions. When isolated-form slots are enabled, separate Playwright profiles can prepare ordinary fields within the original three-submission-workers-per-pipeline cap. A worker preparing its private profile does not steal an active company auth owner. Actual sign-in/email verification and final actions still respect the company mutex, retaining unambiguous token ownership.

The measured workload used the actual scheduler's reservation logic, the same two existing worker IDs/pool size, separate real Chromium profiles and identical fictional form work. No extra agents were added. Shared/native exclusion, the three-worker cap, no auth-owner transfer and final-action blocking behind another auth owner were verified separately.

Evidence: `form-slots.json` and `tests/test_isolated_form_slots.py` plus the existing authentication/recovery suite.

## Verification, deployment and reproducibility

- `python -m pytest -q --tb=short`: **335 passed**, one pre-existing Starlette/httpx deprecation warning.
- `python tools/browser_efficiency_acceptance.py --area lifetime --output ...`: real browser disconnect/termination/host-exit preservation and separate-owner isolation passed.
- `python tools/browser_efficiency_acceptance.py --area batching --output ...`: actual field readback and call counts passed.
- `python tools/efficiency_ablation_measure.py`: context/source measurements and two-worker browser scheduling comparison passed.
- `python tools/optimization_model_measure.py`: four bounded actual DeepSeek/CLI synthetic measurements. `--resume` reuses explicit saved measurements after a harness-only validation adjustment. No actual keys are put in argv or results.
- `python tools/verify_efficiency_preservation.py --output ...`: all original values in 44 production tables identical before **and after** deployment; SQLite quick check `ok`, zero active workers.
- Dashboard restarted successfully; health `ok`; worker-settings revision remains 1 with the same DeepSeek low/high role choices. No campaigns resumed, and monitoring remains paused after the earlier bounded trial.

Full numeric results, invocation records, image hashes, deployment state and preservation hashes live under `data/experiments/five-area-improvements/`, with `results.json` as the combined index. The baseline source/database snapshot is under its timestamped baseline folder. These local files contain no provider key; benchmark outputs use synthetic applicant data, while the historical-layout copies remain local with the existing applicant artifacts.

The four model measurements together cost approximately $0.00895 at the previously verified DeepSeek peak token rates; this excludes supervisor work and is not an invoice. No live-campaign throughput claim is made. The next real campaign can provide a larger outcome-quality and latency sample using the new instrumentation without conflating fixture results with employer behavior.

# Five-area pipeline improvements

Goal: implement and independently measure persistent browser state, fewer model/tool round trips, smaller cache-stable context, fewer document rewrites, and isolation-aware browser scheduling. Applicant truth, exact human approvals, uncertain-send handling and receipts remain mandatory. All real campaigns stay stopped during implementation and local measurements.

Baseline: commit `22847b0`; source and SQLite snapshot saved under `data/experiments/five-area-improvements/20261028T102103Z-baseline/`. The previous xAI trial and comparison remain unchanged.

| Area | Implementation target | Required independent evidence |
|---|---|---|
| 1. Browser lifetime | A worker-owned Playwright service survives CLI disconnection, dashboard restart, ordinary waits and quota pauses. A live but slow service is never replaced. Explicit terminal cleanup closes only its owner. | Local real-browser process disconnect/reconnect test preserves unsaved values, selected file and page state; separate worker cannot see it; dead-service recovery is identified as a new browser generation. Baseline stdio loses the same transient state. |
| 2. Mechanical batching | One tool applies an agent-chosen batch of compatible field operations and returns one observation. Validation, stale refs and partial errors stop the batch. Final application sends, permissions and binding actions remain separate. | Same fictional form and values before/after; independent model-facing call counts, returned payload sizes, browser time and final state checks. No semantics delegated to a scripted job evaluator. |
| 3. Context/cache | Runtime-specific prompts, narrow tool descriptions, source-handle reuse and bounded resumable state reduce repeated content without aggressive forced compaction. | Stable-prefix identity and source retrieval tests; before/after prompt/schema/output sizes and actual model token counters where available. Cache/uncached counts kept distinct; no token claims based solely on character counts. |
| 4. Documents | Use existing templates and precise edits; separate review acknowledgement from republishing; retain actual rendered PDF review and immutable versions. | Saved/synthetic resume before/after with same facts and layout; measured publish/render calls, output payload and model output tokens. Actual preview inspection remains required. |
| 5. Isolation-aware slots | Separate Playwright profiles may prepare forms within existing worker caps; native shared sessions and truly shared authentication retain their locks. | Scheduler tests prove concurrent isolated preparation, shared-session exclusion, exact role caps and email ownership. Measured local two-worker workload with only this switch changed. |

Measurements use feature switches and identical local fixtures with one area changed at a time. Report mechanism-level measurements separately from actual model timings, include failures and missing counters, and avoid generalizing synthetic results into guaranteed employer throughput. No production application will be sent as a benchmark.

Completion requires all five implementations, focused regressions plus an appropriate full suite, real-browser acceptance, independent measurements, preserved production data, deployment without resuming campaigns, committed/pushed code and a requirement-by-requirement evidence report.

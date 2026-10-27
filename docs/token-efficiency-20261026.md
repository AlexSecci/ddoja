# Token-efficiency update — 26 October 2026

The pipeline now sends less repeated evidence and limits work accumulating behind submissions. Agents still interpret complete descriptions and judge fit; model choices, reasoning effort, and the one-JD-per-evaluator design are unchanged. Application campaigns remained stopped throughout this work.

## Measured model usage

Three independent evaluations used identical saved JDs and applicant sources, with Sol 6.1 at high effort in both arms. The cases cover a C++ systems fit, an unrelated mechanical role, and an infrastructure stretch opportunity. Browser access was disabled in both benchmark arms; no employer received anything.

| Counter | Before | After | Reduction |
| --- | ---: | ---: | ---: |
| Input tokens, including cached input | 635,990 | 380,087 | 40.24% |
| Uncached input tokens | 138,966 | 99,383 | 28.48% |
| Cached input tokens | 497,024 | 280,704 | 43.52% |
| Output tokens, including reasoning | 3,289 | 2,776 | 15.60% |
| Tool calls | 10 | 7 | 30.00% |

All three decisions were preserved, with supported systems experience and material uncertainties retained. The approved stretch case continues to report AWS depth and sponsorship as unresolved. General professional experience is not misrepresented as years of professional C++.

This is regression evidence, not a statistical guarantee of unchanged judgment across all future jobs. Subscription quota weighting is not exposed, so these percentages are not measured allowance savings. The final sample took 133.28 seconds versus 110.56 seconds before; an end-to-end speedup is **not** established. Earlier samples varied. The benchmarks measure evaluation, not live submission throughput.

The [aggregate evidence](../data/integration/20261026-token-results.json) links conceptually to the immutable input hashes, actual counters and full findings in [before](../data/integration/20261025-tokens-baseline.json) and [after](../data/integration/20261025-tokens-final.json). Subsequent template placement keeps identical data available outside the cacheable prefix; protocol tests verify that adjustment without another inference run.

## Changes

- The applicant index shrank from 69,327 to 32,500 characters. It retains canonical Yes/No answers and exact experience depth. Duplicate heading paths, excerpts, aliases, full question histories and repeated provenance were replaced by exact retrievable handles. Original sources and full answers remain intact, with their provenance and scope.
- `get_context` can retrieve up to 16 agent-selected source handles while checking live controls, avoiding a separate controls-only roundtrip. Full documents, sections and fact records remain retrievable; the total retrieval size limit still applies.
- Stable instructions and applicant evidence precede worker IDs, revisions and job details. Changing document bases are supplied afterward as `document_templates`, avoiding avoidable cache-prefix changes. Saved sessions no longer append the full unchanged core prompt on every resume.
- Scheduling prioritizes document/submission work over new evaluations. Normally, six outstanding approved submissions buffer further evaluations; a known connector outage lowers that buffer to three. Existing concurrent evaluations can finish. Discovery yields behind a full submission buffer when at least twelve evaluations are pending. Nothing is discarded or semantically filtered by these limits; ordinary completion/answer/resume scheduling wakes the same saved workers without model polling.
- Identical JD re-observations no longer create new versions or invalidate in-flight assessments. Actual content/metadata changes still invalidate them. Description repair preserves employer metadata and keeps observation provenance in immutable events.
- A failed connector owner is reported as a recovery blocker. A queue held by unanswered application checkpoints is reported as waiting for user input rather than silently appearing to run.

Existing exact evaluation caches, batched listing identity checks, browser capture handles, PDF render caching, verified resume bases and shared authentication remain in place. No additional routing/review agent or separately billed API route was added.

## Rejected experiment

Forcing native history compaction at 64,000 tokens reduced input from 748,032 to 564,009 in a seven-turn fictional replay, but increased uncached input from 286,208 to 440,745. Four real compactions appeared in the saved rollout, and the three durable facts survived every turn. Cache disruption and compaction work outweighed the apparent input reduction. **This setting was removed; production retains native compaction defaults.** See the [experiment](../data/integration/20261026-history-acceptance.json).

## Verification and operation

The isolated suite passes 250 tests, including exact source restoration, cross-role restrictions, provenance, cache invalidation, backpressure release, unchanged-JD commits, role concurrency and uncertain-send protections. [Test output](../data/integration/20261026-token-tests.txt).

All original values across 43 database tables match the pre-change backup; SQLite quick check is `ok`. This includes applicant facts, historical applications, receipts, approvals, questions and checkpoints. [Preservation audit](../data/integration/20261026-token-preservation.json).

Ordinary offline tests make no inference calls:

```powershell
.venv\Scripts\python.exe -m pytest -q
```

Optional model benchmarks explicitly consume subscription usage:

```powershell
.venv\Scripts\python.exe tools/token_acceptance.py --label comparison --database data/backups/20261025-before-token-reduction.sqlite --run-subscription
.venv\Scripts\python.exe tools/history_acceptance.py --run-subscription
```

Startup still does not resume application campaigns. Resume a saved company explicitly when ready. The global connector failure remains an actual recovery checkpoint; token optimization does not assert that a browser has recovered or authorize replaying an uncertain submission.

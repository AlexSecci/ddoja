# Human approval batches

Inbox groups current action approvals by campaign. Nothing is selected initially.
Select individual requests or the current list (up to 100), review their exact action,
terms and documents, then approve or reject the selected requests with one click.
Current browser-recovery requests can join the selection; superseded ones cannot.
First-application reviews remain individual.
New requests never join an existing selection automatically.

The server validates all selected IDs and scope fingerprints in one SQLite write
transaction. If any request changed, resolved, became stale, belongs to another
campaign, or its campaign stopped, nothing in the batch is decided. Existing job,
applicant-profile and artifact-version guards apply. The event history records the
batch and each exact decision. An active emitting worker finishes its current turn
before resuming; a waiting worker keeps its saved session.

Batch approval covers those requests only. A worker must verify the same current
action, destination, terms, documents and (where applicable) presented challenge
before acting. Changed or newly presented checkpoints require a new human decision.
Workers cannot approve their own requests or impersonate the applicant.

Timing for resumed workers is documented in `runtime-timing.md`. Browser latency is
measured separately from other tools and time outside tools; the latter includes
model, network and orchestration time, not pure inference alone.

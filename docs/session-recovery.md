# Recovering after a quota pause

Agent memory and the employer website session are separate. The pipeline keeps
the same saved worker session, job, facts, files, approvals and progress marker.
An explicitly resumed quota/campaign issues a new bounded browser-recovery epoch
and marks saved application pages for refresh. Pending inbox items stay pending;
stopped campaigns and possibly sent applications are not replayed.

Before editing an old form, the worker reloads or reopens the matching draft from
the actual applicant dashboard. Company sign-in is serialized through its existing
mutex and uses the authorized saved-password/email-code flow only when needed.
The worker verifies the intended account, requisition and persisted answers, then
acknowledges a new current CUA observation through `complete_auth`. The host blocks
final submission until this acknowledgement. Workers cannot clear this guard by
writing checkpoint notes. A new CAPTCHA or changed terms keep their action-time
confirmation; unchanged exact approvals remain recorded.

Refresh progress belongs to its new epoch, so an old spent tab/error retry does not
prevent recovery after hours offline. One reload and one dashboard reopening are
allowed per epoch. An ongoing provider outage or form validation error can still
fail; this mechanism does not extend employer-controlled session lifetimes or
guarantee that every generic error was caused by session expiration.

For Amazon, refresh completion requires browser-reported evidence from this
requisition's actual `/applicant/jobs/<id>/apply` or `/summary` page. A profile
or dashboard confirms identity only and cannot clear the form-refresh guard.
Authentication logs retain that validated URL without query parameters; page
contents, credentials and verification codes remain redacted.

A pre-submit `Input.dispatchMouseEvent` timeout is handled within the affected
job. The worker first observes whether the action already changed the field. If
needed, it gets one documented keyboard alternative for the same authorized
input, then verifies the result. This host-managed budget cannot be reset through
checkpoint notes. An explicit browser denial still follows its approval flow;
an input timeout after a possibly sent final action permits reconciliation only.
Exhausting local recovery fails that job without locking the whole connector.

`efficiency.max_concurrent_form_writers` limits active submission turns for each
company across campaigns. The current setting is one; document children and
evaluators can still work in parallel, and waiting for documents releases the
writer slot. A genuinely shared connector fault remains a shared gate. If its
owner exits without an actionable inbox item, the dashboard reports a browser
fault rather than falsely claiming it is waiting for the user.

After a possible send, a known `Page.getFrameTree` or `Runtime.evaluate`
timeout now stays local to that job. The worker may make one bounded read-only
observation (a documented screenshot can replace a failed DOM read), then must
record a verified receipt or preserve an uncertain outcome. This uses existing
application scope, records no human approval, opens no global connector gate,
and grants no fresh tab or Submit replay. Explicit browser denials, CAPTCHA,
binding agreements and new permissions retain their own approval requirements.

Stopping a campaign preserves its saved worker sessions, drafts, receipts and
uncertain sends. Restarting the dashboard does not resume that campaign. Only an
explicit later resume should issue a new recovery epoch and schedule its work.

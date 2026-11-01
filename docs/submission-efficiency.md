# Submission efficiency and email recovery

Workers keep an immutable, role-specific instruction and tool contract in ignored
`data/worker-contracts`. Editing another role's prompt no longer invalidates a
running worker's context. New workers capture the latest contract. Changing a
browser backend captures a new appropriate contract. Store permissions, current
applicant evidence, action scopes and stop controls remain live and authoritative.

DeepSeek Responses requests use a per-invocation loopback relay to the official
provider. It canonicalizes JSON keys and tool ordering, streams responses, and
persists actual response usage before forwarding terminal events. It records
hashes and numeric metadata, never prompts, tool contents, email bodies or keys.
`agent_provider_requests` contains individual data points. The dashboard uses
these counters instead of overlapping CLI counters for covered invocations.
Missing responses remain explicit lower bounds. Cached input is included in
input, and reasoning is included in output. HTTP duration includes provider and
network work; it is not a measurement of pure inference. Existing worker timing
separates browser/tools, other waits and model/transport intervals.

At a safe pre-send boundary, a submission context above the configured input
budget can start a compact model session on the same worker and owned browser.
Its checkpoint, actual approvals, prepared documents and send history survive.
A possibly sent or uncertain application cannot rotate this way. A committed
submission receipt is idempotent and takes precedence over later transport errors.
Browser cleanup belongs to the host after the receipt; no model cleanup turn is
required. Only a current exact negative Greenhouse code-rejection observation
can enable bounded same-step recovery. Unknown outcomes remain reconciliation-only.
An initial Submit whose only attempted click failed native reference lookup can
also recover once, provided the exact error proves the click never executed,
there were no other mutations after authorization, the same owned browser remains
live, and a fresh exact-page snapshot exists. Timeouts and disconnected outcomes
do not qualify. Uploads include a fresh inline snapshot so the agent receives
the references created after attachment.

`config/submission-efficiency.json` configures the context budget and bounded
email handoff waits. These are checkpoint waits, not overall application time
limits. Late delivery wakes the same task without repeated model polling.

## Optional Gmail read-only broker

1. In Google Cloud, create a project and enable the Gmail API.
2. Configure Google Auth Platform: External audience, your own support/contact
   email, and your Gmail account as a test user.
3. Add only `https://www.googleapis.com/auth/gmail.readonly` under Data Access.
4. Create a Desktop app OAuth client and download its JSON to a private folder.
5. Run from the project folder:

   ```powershell
   .venv\Scripts\python.exe tools\configure_gmail_broker.py --client-file C:\private\desktop-client.json --expected-email applicant@example.invalid
   ```

6. Complete Google's account selection and read-only consent. The tool verifies
   the configured applicant mailbox and encrypts offline credentials with Windows
   user-bound DPAPI in ignored `data/secrets/gmail-readonly.dpapi`.

Setup does not read messages. At an actual current owned application checkpoint,
the broker queries a narrow Greenhouse sender/subject/time window, checks sender,
recipient, nonce and auth ownership, and rejects ambiguous matches. It never
writes mail. Other issuers and an unconfigured broker retain the existing scoped
Codex supervisor fallback. Gmail retrieval grants no application-send consent.
Required binding/challenge approvals remain human decisions in the inbox.

Google's External/Testing refresh tokens expire after seven days for Gmail scopes.
Reauthorization or an appropriate publishing setup is needed for longer use.
See Google's [Gmail quickstart](https://developers.google.com/workspace/gmail/api/quickstart/python)
and [OAuth token expiration rules](https://developers.google.com/identity/protocols/oauth2#expiration).

No live application runs during installation or local tests. Trials must explicitly
select new, unsubmitted jobs; the snapshot and per-response usage support before/
after comparison. Published token-price estimates are not provider invoices and
exclude supervisor/subscription work.

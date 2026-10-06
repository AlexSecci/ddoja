# Browser permission mismatch

Observed October 6, 2026, after user-supplied screenshots at 19:22 Europe/Rome. This report contains no credentials.

## Approved operation

A bounded GPT-6 Luna worker, authenticated through the existing ChatGPT subscription, creates its own Chrome tab for `http://127.0.0.1:8765/practice`, fills fixed fictional values, uploads only `tests/fixtures/fictional-test-resume.txt`, and saves a local fictional receipt. The user explicitly approved this operation. No employer application or real applicant data is involved.

## Settings shown by the user

- Chrome installed and enabled in desktop Computer use settings.
- Agent permissions: Default Browsing = Always allow; Downloads = Always allow.
- No custom site rows visible.
- Full CDP access enabled. The test uses only documented browser APIs and does not use raw CDP.

Only the visible columns are established. No conclusion is drawn about controls outside the screenshots.

## Fresh result

Worker conversation: `01a11215-74df-70b3-b824-689f49a58bd2`.

The worker called `cua.getState()` successfully, then directly attempted the documented Chrome tab creation for the approved URL. The tool rejected it with:

> A saved user permission setting blocks this action. Browser use cannot access http://127.0.0.1:8765 because the user has a saved preference that blocks it.

No tab was created, no field was filled, no file was selected, and no receipt was saved. The full fresh tool result is retained in `data/integration/practice-complete.report.json`. A previous report is retained separately.

A narrow, read-only check of supported configuration found no `browser_use` override in the user configuration and no `.codex/config.toml` in this project. Authentication files, browser private preferences and internal permission stores were not inspected. Managed requirements and connection state have not been established as causes.

## Recovery and limits

Do not repeat unchanged attempts, substitute another browser/origin/port, loosen the command sandbox, edit private permission stores or bypass the tool's decision. Preserve the existing specific fictional-test approval.

The next supported troubleshooting step is for the user to fully quit and reopen the desktop app and Chrome, then report the restart. After that new connection-state update, make at most one fresh direct test in the same worker. If the mismatch remains, collect the new result for `/feedback` rather than claiming browser integration is complete. Live application submission remains disabled.

Official reference: [Browser extension troubleshooting](https://learn.chatgpt.com/docs/chrome-extension). Restarting Chrome and the desktop app are documented recovery steps; the documentation does not establish the cause of this particular mismatch.

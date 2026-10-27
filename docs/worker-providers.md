# Worker providers and live settings

Open **Settings** in the dashboard to choose the provider, model, reasoning effort and browser for discovery, evaluation, résumé/cover-letter writing, and submission independently. Press **Save worker settings** to persist them in the local database. Choices survive a dashboard restart.

The default remains the existing subscription setup:

| Stage | Model | Effort | Browser |
|---|---|---|---|
| Discovery | Luna 6 | medium | Chrome / native CUA |
| Evaluation | Sol 6.1 | high | Chrome / native CUA if needed |
| Documents | Sol 6.1 | high | none |
| Submission | Luna 6 | medium | Chrome / native CUA |

DeepSeek uses the official Responses API through the same Codex CLI. The integrated model is `deepseek-flash` (currently V4.1 Flash 1017); its supported efforts are **low, high, max**. This is a rolling provider alias, not a frozen model version. Unsupported provider/model/effort combinations are rejected. No model call is made to populate settings or check local installation readiness.

## When a saved choice takes effect

Each reserved invocation gets an execution snapshot. Saving settings does not interrupt or modify a running invocation. Its next start/resume uses the saved settings, including after a document wait, question, approval, interruption, or explicit quota resume.

A model/effort change on the same provider keeps the saved CLI session where possible. A provider, browser, or execution-transport change starts a fresh model session with the task's durable checkpoint and records the previous session in its history. Browser state must be observed again before input. Changing provider does not grant approval, erase retries, revive terminal tasks, or reset a possibly sent application. Possibly sent applications retain their original execution configuration until reconciliation.

Saving settings never starts or resumes a campaign, clears a quota latch, or retries an uncertain send. The existing conservative **global quota pause** is retained: changing to a paid provider does not bypass a recorded pause. Resume remains a separate explicit user action.

## DeepSeek key

Save a key in Settings, or run `.venv\Scripts\python.exe tools\configure_provider_key.py` for a hidden console prompt. The saved key is protected with Windows DPAPI for the current user at `data/secrets/deepseek.dpapi`; the dashboard never returns it. Only DeepSeek worker subprocesses receive it. Shared Codex subscription login and global configuration are unchanged. Alternatively, set `DEEPSEEK_API_KEY` in the dashboard service environment; a saved key takes precedence. Removing a saved key does not remove an environment key.

## Playwright

DeepSeek browser workers use the independent Playwright MCP runtime. OpenAI workers can use either native CUA or Playwright. Install the pinned runtime from the project folder:

```powershell
npm ci --prefix browser-runtime --ignore-scripts --no-audit --no-fund
```

Installed Chrome or Edge and Node.js are required. Each worker gets a separate persistent, headless browser profile under `data/browser-profiles/`. It does **not** inherit personal Chrome passwords, email sign-in, cookies, extension permissions, or native tab groups. A provider/browser change can therefore require an ordinary sign-in handoff. Do not extract or copy personal browser credentials. The existing scoped inbox/authentication flow is retained; exact binding-term/CAPTCHA approvals remain human decisions.

The runtime exposes navigation, accessibility snapshots, form entry, file chooser upload, screenshots, tabs, dialogs and close. Browser JavaScript execution and arbitrary Playwright code are not exposed. Browser failures are isolated to that worker's Playwright profile instead of opening the global native CUA connector circuit. Native CUA remains available and unchanged as a selectable backend; its previously observed DeepSeek host-access failure has not been fixed or bypassed.

Actual Playwright MCP results feed the same listing-capture, approval evidence, receipt and uncertain-send checks as native browser results. Tool durations are measured as browser time. New usage records include their actual execution snapshot; `/api/efficiency` reports usage grouped by execution as well as role. Historical rows without a recorded choice remain explicitly unattributed.

## Verification

`pytest` uses isolated stores and synthetic runtimes to verify active-run pinning, switching both ways, saved-session preservation, supported choices, optimistic settings revisions, credential secrecy and scoped browser evidence. An optional paid integration check exercises the real CLI, official DeepSeek API, Playwright navigation and agent-authored listing capture against a loopback fictional page:

```powershell
.venv\Scripts\python.exe tools\provider_smoke.py --run-paid-test
```

It uses a separate synthetic database and browser profile, never the production campaign ledger, and writes its report under `data/provider-smoke/`. No employer receives an application.

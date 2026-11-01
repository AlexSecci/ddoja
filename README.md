# DDoJA

**Distributed Dispatch of Job Applications**

DDoJA is a local dashboard and agent orchestration system for discovering jobs, evaluating fit, preparing truthful application materials, and tracking submissions with verifiable receipts.

## How it works

- **Discovery** records full job descriptions and explains exclusions against the campaign filters.
- **Evaluation** scores applicant fit from 0 to 100 with requirement-level evidence. Confirmed hard barriers block a role; eligible roles enter the backlog in descending score order.
- **Materials** workers write and render tailored résumés and cover letters when a real application form requires them.
- **Submission** workers retain owned browser sessions, recover from interruptions, and preserve uncertain sends for reconciliation rather than silently submitting twice.
- **The dashboard** provides independent campaign and stage controls, a scoped question/approval inbox, job records, and per-task token and timing metrics.

Provider, model, reasoning effort, speed mode, and browser backend are configurable by worker role. Codex subscription models and the official DeepSeek API are supported. New settings apply at invocation boundaries; existing workers retain their execution snapshot. Playwright and native computer use remain selectable.

The host handles persistence, worker scope, current evidence versions, ownership, deduplication and approvals. Agents interpret job requirements and applicant evidence. Human approval checkpoints remain tied to the exact current action and materials.

## Local setup

The current desktop launchers target Windows and require Python 3.11+, Node.js, Git, and an installed, signed-in Codex CLI.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
npm ci --prefix browser-runtime
.\run.ps1
```

Open the dashboard at `http://127.0.0.1:8765`. Start campaigns and stages explicitly from the dashboard. Restarting the service preserves checkpoints and does not automatically resume a stopped campaign.

For a background dashboard, use `.\launch-dashboard.ps1`. Run `.\install-desktop-shortcut.ps1` to install the **DDoJA** desktop shortcut. Existing shortcuts continue to work.

### Machine-specific rendering tools

`config/runtime.json` contains portable defaults. Put local executable/font paths in **`config/runtime.local.json`**, which overrides those defaults and is ignored by Git. Paths support `~` and environment-variable expansion. PDF generation also requires the configured document Python dependencies, Poppler and the PDF skill marker; missing dependencies are reported before rendering.

### Credentials and applicant data

Configure provider credentials locally using the dashboard or the private setup tools. API keys and Gmail OAuth credentials are encrypted for the Windows user under `data/secrets/`. Optional Gmail read-only setup requires your own Google Desktop OAuth client:

```powershell
.\.venv\Scripts\python.exe tools\configure_gmail_broker.py --client-file C:\private\desktop-client.json --expected-email YOUR_GMAIL_ADDRESS
```

Import applicant documents through the local application. Databases, applicant documents, browser profiles, snapshots, generated materials, receipts, runtime logs and credential files belong in ignored local storage. Do not commit personal application evidence or credentials. The public repository contains source code and fictional test fixtures.

## Development

```powershell
.\.venv\Scripts\python.exe -m pytest -q
node --check app/static/app.js
node --check app/static/settings.js
gitleaks git . --config .gitleaks.toml --redact --log-opts="--all"
```

The default branch is the saved application baseline; `codex/DDoJA-dev` is the development branch for experiments. Keep live campaign state local when switching branches.

## Documentation

- [User guide](docs/user-guide.md)
- [Agent architecture](docs/agentic-architecture.md)
- [Worker providers and browser backends](docs/worker-providers.md)
- [Usage and timing metrics](docs/runtime-timing.md)
- [Session recovery](docs/session-recovery.md)
- [Submission efficiency and Gmail broker](docs/submission-efficiency.md)
- [Fictional end-to-end fixture](docs/agentic-fixture-acceptance.md)

Runtime evidence and private acceptance reports are retained locally and are not part of the public source tree.

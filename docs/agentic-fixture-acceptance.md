# Isolated agentic browser fixture

This harness is for an explicitly supervised local acceptance run. It is not part of normal startup and it does not contact an employer. Preparing it creates a new temporary SQLite workspace, copies only the agent code/prompts/config needed by that workspace, and seeds a fictional Test Candidate profile. It does not create a company or start a pipeline.

From the project root, `python -m tools.agentic_fixture_harness prepare` creates the workspace and prints its absolute path. The module invocation is required so Python can import the application package. The workspace is marked `AGENTIC_FIXTURE_TEST_ONLY.json`; do not point it at the project folder or an existing workspace. Start it with `python -m tools.agentic_fixture_harness serve --workspace <that-path>`. It binds only to `127.0.0.1:8765`, uses the isolated database, enables HTTP careers URLs only for the exact fictional board URL in that app instance, and mounts the fictional site. The normal `run.ps1` application remains HTTPS-only and does not load the fixture routes.

The local board has three fictional listings: a Chicago hybrid C++ vehicle-data role, a remote-only systems role, and a mechanical role. The only application form has synthetic contact and work-authorization fields, a resume PDF upload, and a cover-letter text area. It contains no CAPTCHA or binding legal terms. The receipt route accepts only the fixed synthetic Test Candidate values and a `resume.pdf` whose SHA-256 matches a current ready resume artifact for that fixture job. It writes a receipt with `test_only: true` under the temporary workspace and records the cover-letter hash without storing its text. The route does not change the pipeline's submission status; only the scoped submission agent can do that after a real browser result and the durable final-action guard.

After the fixture workspace is served and the rendered start test is authorized, start one pipeline through the dashboard using:

- Company: `Fictional Mobility Systems`
- Careers URL: `http://127.0.0.1:8765/__fictional/board`
- Role title: `Software Engineer`
- Location: leave blank

The worker agents decide selection, fit, form requirements, and whether the form requests both documents; the harness does not hard-code those decisions. The role filter should leave one of the three observed listings selected under the fixed United States/full-time/on-site-or-hybrid requirements. Watch the rendered card and job-local inbox. Stop at the first question, first-application review, or action-approval checkpoint. Resolve only a checkpoint that has been specifically approved. The final fixture click and PDF upload require their own direct authorization after the current form and exact action are reviewed. Do not use this harness for employer credentials or real applicant data.

Offline fixture tests use the real Store, agent manager, scoped MCP bridge, and PDF renderer with an injected deterministic synthetic runtime. They verify scheduling and persistence, review artifact variants, the fictional page, receipt validation, and file-hash binding without model calls or a browser-controlled upload. Keep those tests distinct from any rendered run. Preserve the receipt, pipeline ID, worker states/sessions, and exact user-visible checkpoints from an actual browser run.

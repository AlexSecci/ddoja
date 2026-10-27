"""Bounded local form test. File selection is a separate, explicitly approved phase."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.cli_paths import resolve_codex_cli

try:
    from .integration_probe import run_probe
except ImportError:  # Executed as `python tools/browser_practice.py`.
    from integration_probe import run_probe

SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["prepared", "confirmed", "failed"]},
        "tab_id": {"type": "string"},
        "page_url": {"type": "string"},
        "name_value": {"type": "string"},
        "email_value": {"type": "string"},
        "sponsorship_value": {"type": "string"},
        "receipt_id": {"type": "string"},
        "detail": {"type": "string"},
    },
    "required": ["status", "tab_id", "page_url", "name_value", "email_value", "sponsorship_value", "receipt_id", "detail"],
    "additionalProperties": False,
}


def _approval_matches(approval, *, expected_file, tab_id=None):
    """Match the saved local-test authorization without inventing a new scope."""
    return (
        approval.get("action") == "local_practice_upload_and_submit"
        and approval.get("destination") == "http://127.0.0.1:8765/practice"
        and approval.get("file") == str(expected_file)
        and approval.get("status") == "approved"
        and approval.get("tab_id") in (None, tab_id)
    )


def _classify_file_input_value(value, expected_file):
    """Classify the DOM-backed file input value without trusting FileList readback."""
    if not isinstance(value, str):
        return "ambiguous"
    if value == "":
        return "empty"
    basename = value.replace("\\", "/").rsplit("/", 1)[-1]
    return "matches" if basename == Path(expected_file).name else "ambiguous"


def _file_selection_prompt(expected_file, *, allow_existing_match):
    """Build the bounded file-input instruction for a new or resumed practice tab."""
    file_name = Path(expected_file).name
    set_files = f"chooser.setFiles([{str(expected_file)!r}])"
    inspect_value = (
        "Read the file input's DOM-backed `value` with locator.evaluate. Derive only the basename "
        "by replacing backslashes with slashes and taking the final path segment; do not rely on "
        "files.length, `.files[0]`, body text, or the native widget. Emit the exact page URL, "
        f"safe observed basename, whether the value is empty, and whether the basename exactly "
        f"matches `{file_name}` through nodeRepl.write before branching. Keep every JavaScript "
        "variable local to the current CUA tool call; a later call may have a fresh lexical scope. "
        "Re-read the live input value in the same call that uses it, and never reference an unbound "
        "observation variable from an earlier call or turn. "
    )
    if allow_existing_match:
        return (
            inspect_value
            + f"If the basename already exactly matches `{file_name}`, do not reselect the file; "
            "continue to the single Save action and let the local server validate the exact file "
            "contents/hash and email. If the value is empty, and only if it is empty, register "
            "waitForEvent('filechooser') before clicking the actual file input, then call the "
            f"documented array form `{set_files}`. Re-read the DOM-backed value, emit the same "
            "compact observation, and continue only if its basename exactly matches the approved "
            f"fixture `{file_name}`. If a nonempty value has any other or ambiguous basename, do "
            "not alter it or choose another file; preserve the tab and return failed. Do not throw "
            "after emitting observations, since that discards the useful CUA result. A fresh access "
            "or permission denial is terminal. "
        )
    return (
        inspect_value
        + "This is a newly created tab, so stop if the file input is nonempty; do not reuse an "
        "unknown selection. For an empty value only, register waitForEvent('filechooser') before "
        f"clicking the actual file input, then call `{set_files}`. Re-read the DOM-backed value, emit "
        f"the same compact observation, and continue only if its basename exactly matches `{file_name}`. "
        "Do not throw after emitting observations. "
    )


def _receipt_confirmation_prompt():
    """Confirm one asynchronous save with bounded read-only DOM observations."""
    return (
        "Click Save local test receipt exactly once. Treat that click as possibly sent immediately; "
        "never click Save a second time. After the click, remain on the same exact tab and poll only "
        "read-only DOM state for at most 30 seconds (up to 30 observations, one second apart). On each "
        "observation read the current document URL, `#practice-confirmation h2` textContent, `#receipt-id` "
        "visible text, `#receipt-detail` textContent, and `#status` visible text. If the receipt is not "
        "ready yet, wait with `await new Promise(resolve => setTimeout(resolve, 1000))` before the next "
        "read-only observation; do not issue another browser action while waiting. Stop early on a visible "
        "validation error. Parse the 32-hex ID from the actual visible `Receipt:` line. Parse the filename "
        "from the JSON currently rendered in `#receipt-detail`; never insert the expected filename as an "
        "observation. Confirmation requires actual current values for URL, heading text, receipt ID and "
        "filename. Emit one compact JSON observation through `nodeRepl.write` containing those actual "
        "values and the tab ID. Include the literal heading and filename values obtained from those DOM "
        "reads so the successful CUA result contains their evidence. If the DOM is initially blank, keep "
        "polling within the bound. If the confirmation never appears or any read fails after Save may have "
        "been sent, do not resubmit; call `tab.markHandoff()` and return failed with the actual reason. "
        "After a fully verified receipt, call `tab.markHandoff()` and return confirmed. The controller must "
        "persist and validate the canonical report before any separate deliverable or cleanup action. "
        "Do not call `tab.markDeliverable()` in this phase. Do not throw after emitting observations."
    )


def _phase_timeout(phase, requested=None):
    """Keep verification short while allowing a longer bounded full-model regression."""
    if phase == "verify":
        if requested not in (None, 120):
            raise ValueError("Read-only verification is capped at 120 seconds.")
        return 120
    if requested is None:
        return 180
    if requested not in (120, 180, 300):
        raise ValueError("Practice timeouts must be 120, 180, or 300 seconds.")
    return requested


def _permission_note_required(phase):
    """Only a preparation retry needs a reported connection change; complete is a fresh approved test."""
    return phase == "prepare"


def _submit_prompt(tab_id, expected_file):
    return (
        "Read agent.documentation.get('file-uploads') and "
        "agent.documentation.get('local-web-development') before interacting with the page. "
        "Initialize cua_repl with exactly await cua.getState(); as the first CUA API call in this "
        "invocation. From that fresh inventory, find the current Chrome browser ID and verify that "
        f"the exact worker-owned tab {tab_id} is present. If it is absent, stop and return failed; "
        "do not create a replacement tab. Do not inspect any other tab. Use cua.getBrowser({id: "
        "<current Chrome ID>}) and that Browser's tabs.get(" + repr(tab_id) + ") to bind the same "
        "owned tab's rich documented Tab API. Do not use the wrapper returned by createBrowserTab, "
        "because this is a resumed tab. Verify the current page URL is exactly "
        "http://127.0.0.1:8765/practice before changing any fields; stop on a different URL or a "
        "browser security interstitial. Do not assume any form field was filled in an earlier turn. "
        "For Applicant name, use locator.evaluate to compare the current value to 'Test Candidate'. "
        "If it does not match, fill exactly 'Test Candidate' once and verify again; stop before upload "
        "if the exact match cannot be confirmed. For Email, use locator.evaluate to emit only "
        "`email_matches` and `email_nonempty` booleans, without returning or echoing the value. If the "
        "field is empty, fill exactly 'candidate@example.test' once and recheck. If it is already "
        "nonempty but the readback is masked or still mismatches, do not overwrite it or type a "
        "redaction marker; report the readback limitation and rely on the local server's exact-value "
        "validation. Stop before upload if the control remains empty. The server validates the exact "
        "approved email when saving the receipt; any validation error is terminal, with no second Save. "
        "For Requires US sponsorship?, read the live DOM options and selected value; verify visible "
        "label `Yes` maps to value `yes`, selectOption('yes') only if needed, then verify again. Stop "
        "before upload if this exact option cannot be confirmed. "
        "Emit only compact booleans `name_matches`, `email_matches`, `email_nonempty`, and "
        "`sponsorship_matches` through nodeRepl.write; never echo the email. Unrelated background "
        "telemetry/network diagnostics do not prove the local page is inaccessible. Continue when "
        "supported CUA calls succeed and this exact page is usable; stop only on an actual supported "
        "API failure, permission denial, security interstitial, or incorrect URL. Verify the exact page "
        "URL again before upload and before Save. "
        "The saved approval authorizes only this exact local fictional test: uploading only "
        f"{expected_file} to http://127.0.0.1:8765/practice and saving a local receipt. It does not "
        "override browser permissions. "
        + _file_selection_prompt(expected_file, allow_existing_match=True)
        + _receipt_confirmation_prompt()
        + " Do not retry through another route or use HTTP, CDP, another browser, or another tab."
    )


def _verify_prompt(tab_id):
    """Build a strictly read-only confirmation check for an owned practice tab."""
    return (
        "This is a strictly read-only receipt verification. Do not navigate, create or close tabs, "
        "fill or select fields, open a file chooser, upload files, click controls, or submit anything. "
        "Your first CUA API call in this invocation must be exactly `await cua.getState();` by itself. "
        "Use only that fresh inventory to identify the current Chrome browser ID and check whether the "
        f"exact supplied practice tab {tab_id} is listed. Do not inspect any other tab. If it is listed, "
        "bind only that exact tab with the documented wrapper `cua.getTab(tabId, {browser: browserId})`; "
        "do not use Browser.tabs.get, create a new tab, or open another browser surface. If the tab is "
        "not listed or the wrapper cannot bind it, return failed without trying another ID. Read only "
        "the initial accessible UI state returned for that tab and, if needed, refresh it using the "
        "documented read-only `tab.getAXState()` API. Do not use a saved report, server record, model "
        "memory, or supplied receipt identifier as browser evidence. From the returned AX state, extract "
        "the actual visible heading text, the current visible `Receipt:` line and its 32-hex ID, and the "
        "filename parsed from the current visible receipt-detail text/JSON. Do not copy expected values "
        "from this prompt or return email/other form data. The URL must come from the fresh inventory "
        "entry for this exact tab. Emit one compact JSON observation through `nodeRepl.write` with the "
        "observed URL, heading text, parsed filename, receipt ID, and exact tab ID. The observed values "
        "must be the exact URL `http://127.0.0.1:8765/practice`, heading `Local practice submission "
        "received`, a 32-hex ID, and filename `fictional-test-resume.txt`. Return confirmed "
        "only if all four live DOM checks pass. After emitting the observation, call documented "
        "`tab.markHandoff()` for the controller to persist and validate the canonical report. Do not "
        "mark the tab deliverable in this phase. On every incomplete result or "
        "exception while the owned tab is present, call `tab.markHandoff()` before returning failed. "
        "Do not throw after emitting observations, since that discards the actual CUA evidence."
        " The final structured response must use the supplied schema, with status confirmed only after "
        "the live checks pass, the observed tab ID/URL/receipt ID, empty `name_value`, `email_value`, "
        "and `sponsorship_value` (they are not needed for this read-only check), and a short detail."
    )

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=["prepare", "submit", "complete", "verify"])
    parser.add_argument("--codex", default=resolve_codex_cli())
    parser.add_argument("--tab-id")
    parser.add_argument("--approval-record", type=Path)
    parser.add_argument("--session-id")
    parser.add_argument("--timeout", type=int, choices=(120, 180, 300),
                        help="Bound the CLI turn; use 300 for a full Max regression run.")
    parser.add_argument("--permission-change-note", help="New user-provided permission evidence or exact setting-change report; never inferred.")
    options = parser.parse_args()
    if _permission_note_required(options.phase) and not options.permission_change_note:
        parser.error("A fresh operator-reported browser connection or permission-state change is required before preparing after a previous browser block.")
    base = (
        "Test the local practice form only; use fictional data. No employer pages, accounts, legal "
        "agreements or CAPTCHAs. No shell commands, filesystem searches, external HTTP requests or browser "
        "control outside cua_repl. Its first CUA API call must be exactly await cua.getState(); by itself, "
        "then follow the returned documentation for subsequent calls. Use the connected "
        "Chrome browser, and use documented APIs only. Browser IDs are session-specific; never reuse another "
        "worker's browser ID. Never claim or inspect another user's tab; an explicitly supplied, exact "
        "worker-owned continuation tab may be rebound to its rich Browser.tabs.get surface. "
        "A background telemetry diagnostic for oaistatsig.com, including a 403, is not evidence that the "
        "verified localhost practice page failed. Do not open or interact with that telemetry origin or "
        "change permissions. Continue only while fresh CUA observations show the exact localhost page is "
        "usable and the CUA calls complete. Stop on an actual CUA permission denial or API failure, a local "
        "target-page error/security interstitial, or any incorrect URL. "
        "Observe fresh page state after actions; do not invent API methods. "
    )
    if options.phase in {"prepare", "complete"}:
        prompt = base + (
            "After the inventory call, create your OWN new tab using exactly "
            "await cua.createBrowserTab('chrome', 'http://127.0.0.1:8765/practice', "
            "{sessionName: '🧪 Local form test'}); assign the returned tab. "
            "Do not inspect any existing tab. If a rich Playwright surface is needed for this tab, "
            "select only the current Chrome Browser identified by the fresh inventory and bind only "
            "the exact tab ID returned by createBrowserTab using Browser.tabs.get. "
            "Read agent.documentation.get('local-web-development') before interacting. Use the rich "
            "Tab's documented Playwright DOM locators: fill Applicant name with Test Candidate and fill "
            "Email with candidate@example.test. Verify name equality and email nonempty through locator.evaluate "
            "booleans; do not echo the email because tool output may redact it. Read the current DOM options "
            "for Requires US sponsorship?, confirm Yes maps to value yes, then selectOption('yes') only if "
            "needed and verify the selected value and label with locator.evaluate. Emit only these booleans "
            "through nodeRepl.write. "
            "Read back the visible DOM-backed values and URL, then emit only those observed values "
            "through nodeRepl.write. "
        )
        if options.phase == "prepare":
            prompt += ("Do NOT choose/upload a file or submit. Call tab.markHandoff() so the filled form "
                       "survives for the next phase. Return prepared, the exact tab ID and URL, the observed "
                       "values, an empty receipt_id and a short detail.")
        else:
            if not options.approval_record:
                parser.error("The complete test requires explicit user approval for the local site and fixture upload.")
            approval = json.loads(options.approval_record.read_text(encoding="utf-8-sig"))
            expected_file = ROOT / "tests" / "fixtures" / "fictional-test-resume.txt"
            if approval.get("action") != "local_practice_upload_and_submit" \
                    or approval.get("destination") != "http://127.0.0.1:8765/practice" \
                    or approval.get("file") != str(expected_file) or approval.get("status") != "approved":
                parser.error("Approval does not match the local practice test.")
            prompt = ("The saved approval record authorizes this exact local fictional test: filling the fictional "
                      "fields, uploading only the fixed fixture, and saving a local receipt. It does not change or "
                      "override browser permissions. Use only the newly created worker-owned tab and the current "
                      "Chrome Browser's rich tabs.get surface for its exact returned ID. "
                      + prompt + f"Read agent.documentation.get('file-uploads') before file selection. Use the "
                      "documented Playwright filechooser API. "
                      + _file_selection_prompt(expected_file, allow_existing_match=False)
                      + _receipt_confirmation_prompt()
                      + " If a step fails, call "
                      "tab.markHandoff() and return a structured failed response with the actual observed URL, "
                      "tab ID, current stage and error. Every unfinished return must call tab.markHandoff(). "
                      "Do not throw after emitting observations; preserve them in successful CUA output and the "
                      "structured response. Return confirmed only when the visible receipt is verified; do not "
                      "retry or use another route.")
    elif options.phase == "submit":
        if not options.tab_id or not options.approval_record or not options.session_id:
            parser.error("Submission requires the original session ID, exact tab ID and explicit local-test approval.")
        approval = json.loads(options.approval_record.read_text(encoding="utf-8-sig"))
        expected_file = ROOT / "tests" / "fixtures" / "fictional-test-resume.txt"
        if not _approval_matches(approval, expected_file=expected_file, tab_id=options.tab_id):
            parser.error("Approval does not match this local practice action.")
        prompt = base + _submit_prompt(options.tab_id, expected_file)
    else:
        if not options.tab_id or not options.session_id:
            parser.error("Read-only verification requires the original session ID and exact worker-owned tab ID.")
        prompt = _verify_prompt(options.tab_id)
    if options.permission_change_note:
        prompt = ("LATEST OPERATOR-REPORTED CONNECTION UPDATE, received after the preceding test: " + options.permission_change_note +
                  " Use it only as evidence that connection state changed; it does not grant additional site permission. "
                  "Make one direct documented check of the approved local site under its current permissions. "
                  "Do not report the older denial as a new "
                  "observation. If the tool still reports a block, stop immediately and report that fresh result; "
                  "do not change permissions yourself or use another route.\n\n" + prompt)
    report_suffix = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    try:
        timeout_seconds = _phase_timeout(options.phase, options.timeout)
    except ValueError as error:
        parser.error(str(error))
    report = run_probe(options.codex, "gpt-6-luna", True,
                       timeout=timeout_seconds, persistent=True,
                       resume_session=options.session_id,
                       prompt_override=prompt, schema_override=SCHEMA,
                       stem_override="practice-" + options.phase + "-" + report_suffix,
                       reasoning_effort="max", expected_tab_id=options.tab_id if options.phase in {"submit", "verify"} else None,
                       invocation_id=uuid.uuid4().hex)
    print(json.dumps(report, indent=2))

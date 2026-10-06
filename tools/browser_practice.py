"""Bounded local form test. File selection is a separate, explicitly approved phase."""
import argparse
import json
from pathlib import Path
import shutil

from integration_probe import run_probe

ROOT = Path(__file__).resolve().parents[1]
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

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=["prepare", "submit", "complete"])
    parser.add_argument("--codex", default=shutil.which("codex"))
    parser.add_argument("--tab-id")
    parser.add_argument("--approval-record", type=Path)
    parser.add_argument("--session-id")
    parser.add_argument("--permission-change-note", help="New user-provided permission evidence or exact setting-change report; never inferred.")
    options = parser.parse_args()
    base = (
        "Test the local practice form only; use fictional data. No employer pages, accounts, legal "
        "agreements or CAPTCHAs. No shell commands, filesystem searches, external HTTP requests or browser "
        "control outside cua_repl. Initialize cua_repl with exactly await cua.getState(); as its first "
        "and only API call in that invocation, then follow the returned documentation. Use the connected "
        "Chrome browser, and use documented APIs only. Browser IDs are session-specific; never reuse another "
        "worker's browser ID. Never claim or inspect an existing user tab. "
        "Observe fresh page state after actions; do not invent API methods. "
    )
    if options.phase in {"prepare", "complete"}:
        prompt = base + (
            "After the inventory call, create your OWN new tab using exactly "
            "await cua.createBrowserTab('chrome', 'http://127.0.0.1:8765/practice', "
            "{sessionName: '🧪 Local form test'}); assign the returned tab. "
            "Do not select a browser or bind the dashboard or any other existing tab. "
            "Fill Applicant name with Test Candidate, "
            "Email with candidate@example.test, and Requires US sponsorship? with Yes. "
            "Verify the values using visible DOM-backed input values. "
        )
        if options.phase == "prepare":
            prompt += ("Do NOT choose/upload a file or submit. Mark the tab as a handoff so the filled form "
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
            prompt = ("The human user has now renewed permission for this exact local site after the earlier "
                      "decline: 'Allowed; run the complete local practice test'. They explicitly approved filling "
                      "the fictional fields, uploading only the fixed fictional fixture and saving a local receipt. "
                      + prompt + f"Read file-uploads documentation, then select ONLY {expected_file} using the "
                      "documented filechooser API. Verify its filename and click Save local test receipt. "
                      "Verify Local practice submission received and its receipt ID from the visible page. "
                      "Keep the verified receipt as a deliverable. Return confirmed only when verified; "
                      "otherwise failed with the exact reason, without workarounds.")
    else:
        if not options.tab_id or not options.approval_record or not options.session_id:
            parser.error("Submission requires the original session ID, exact tab ID and explicit local-test approval.")
        approval = json.loads(options.approval_record.read_text(encoding="utf-8-sig"))
        expected_file = ROOT / "tests" / "fixtures" / "fictional-test-resume.txt"
        if approval.get("action") != "local_practice_upload_and_submit" or approval.get("tab_id") != options.tab_id \
                or approval.get("destination") != "http://127.0.0.1:8765/practice" \
                or approval.get("file") != str(expected_file) or approval.get("status") != "approved":
            parser.error("Approval does not match this local practice action.")
        prompt = base + (
            f"The user explicitly approved uploading this exact fictional file to the local practice form "
            f"and saving its local test receipt: {expected_file}. Approved tab ID: {options.tab_id}. "
            "Bind that exact existing Chrome tab; do not create another. Confirm its URL is "
            "http://127.0.0.1:8765/practice and its fields contain Test Candidate, candidate@example.test and Yes. "
            "Read the file-uploads documentation before file selection. Use waitForEvent(filechooser) before "
            "clicking the actual file input, then chooser.setFiles with the exact approved absolute path. "
            "Verify the selected filename, then click Save local test receipt. Verify the visible heading "
            "Local practice submission received and capture the receipt ID. Mark the receipt tab as a "
            "deliverable for review; do not close or alter the user's dashboard tab. "
            "Return confirmed only if the receipt is visibly verified; otherwise failed with the actual reason."
        )
    if options.permission_change_note:
        prompt = ("LATEST HUMAN UPDATE, received after the preceding test: " + options.permission_change_note +
                  " This is new permission evidence from the human. Make one direct documented check of the "
                  "approved local site under its current permissions. Do not report the older denial as a new "
                  "observation. If the tool still reports a block, stop immediately and report that fresh result; "
                  "do not change permissions yourself or use another route.\n\n" + prompt)
    report = run_probe(options.codex, "gpt-6-luna", True, timeout=180, persistent=True,
                       resume_session=options.session_id,
                       prompt_override=prompt, schema_override=SCHEMA,
                       stem_override="practice-" + options.phase)
    print(json.dumps(report, indent=2))

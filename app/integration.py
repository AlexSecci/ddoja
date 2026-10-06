"""Display integration evidence without confusing tool visibility with browser control."""
import json
from .store import digest


def browser_status(root):
    folder = root / "data" / "integration"
    inventory = {}
    try:
        inventory = json.loads((folder / "browser.report.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    available = "Chrome" in inventory.get("final", {}).get("browser_names", [])
    reports = sorted(folder.glob("practice-*.report.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    for path in reports:
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        final = report.get("final") or {}
        receipt_id = final.get("receipt_id", "")
        receipt = {}
        if receipt_id.isalnum():
            try:
                receipt = json.loads((root / "data" / "practice-receipts" / f"{receipt_id}.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
        expected_text = "Fictional test resume.\nTest Candidate\nC++ task scheduling integration fixture.\nNo real applicant information.\n"
        matching_receipt = all(receipt.get(key) == value for key, value in {
            "receipt_id": receipt_id, "test_only": True, "name": "Test Candidate",
            "email": "candidate@example.test", "sponsorship": "yes",
            "filename": "fictional-test-resume.txt", "file_sha256": digest(expected_text)}.items())
        if (final.get("status") == "confirmed" and receipt_id.isalnum()
                and final.get("page_url") == "http://127.0.0.1:8765/practice"
                and matching_receipt):
            state, detail = "local_test_passed", "Local browser fill, upload and receipt verified. Employer workflows remain untested."
        elif final.get("status") == "prepared":
            state, detail = "form_prepared", "Local fields verified. Upload and receipt test pending."
        else:
            failure = json.dumps(report.get("mcp_calls", []))
            if "saved user permission" in failure or "user denied permission" in failure:
                state, detail = "site_permission_required", "Chrome is visible; a saved website permission blocks the local practice test."
            else:
                state, detail = "test_failed", final.get("detail", "Browser control test did not complete.")
        return {"inventory_available": available, "state": state, "detail": detail,
                "verified": state == "local_test_passed", "report": str(path.relative_to(root))}
    return {"inventory_available": available, "state": "control_unverified" if available else "unavailable",
            "verified": False, "detail": "Chrome control has not passed the local practice test."}

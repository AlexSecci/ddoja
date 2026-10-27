"""Display integration evidence without confusing tool visibility with browser control."""
import json
from datetime import datetime, timezone
from pathlib import Path
import time
from .store import digest


def browser_status(root, *, now=None, max_age_seconds=24 * 60 * 60):
    folder = root / "data" / "integration"
    now = time.time() if now is None else now

    def observation(path):
        observed = path.stat().st_mtime
        return {
            "observed_at": datetime.fromtimestamp(observed, timezone.utc).isoformat(timespec="seconds"),
            "fresh": now - observed <= max_age_seconds,
        }

    inventory = {}
    inventory_path = folder / "browser.report.json"
    try:
        inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
        inventory_age = observation(inventory_path)
    except (OSError, ValueError):
        inventory_age = {"observed_at": None, "fresh": False}
    if not isinstance(inventory, dict):
        inventory = {}
        inventory_age = {"observed_at": None, "fresh": False}
    inventory_observation = inventory.get("browser_inventory_observation")
    browser_names, tab_count = [], None
    inventory_valid = False
    if isinstance(inventory_observation, dict):
        names = inventory_observation.get("browser_names")
        count = inventory_observation.get("tab_count")
        canonical = None
        if (inventory_observation.get("operation") == "cua.getState"
                and inventory_observation.get("source") == "successful_cua_repl_js_result"
                and isinstance(names, list) and all(isinstance(name, str) for name in names)
                and isinstance(count, int) and not isinstance(count, bool) and count >= 0):
            normalized = {"browser_names": sorted(set(names)), "tab_count": count}
            canonical = json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            calls = inventory.get("mcp_calls", [])
            successful_call = isinstance(calls, list) and any(
                isinstance(call, dict) and call.get("server") == "cua_repl"
                and call.get("tool") == "js" and call.get("status") == "completed" for call in calls)
            inventory_valid = (successful_call and inventory.get("unapproved_action_events") == []
                               and inventory_observation.get("result_sha256") == digest(canonical))
            if inventory_valid:
                browser_names, tab_count = normalized["browser_names"], count
    available = inventory_valid and "Chrome" in browser_names
    inventory_fresh = inventory_age["fresh"] and inventory_valid

    def practice_observation_valid(report, receipt_id):
        observed = report.get("browser_practice_observation")
        expected = {"page_url": "http://127.0.0.1:8765/practice",
                    "heading": "Local practice submission received",
                    "receipt_id": receipt_id, "filename": "fictional-test-resume.txt"}
        if not isinstance(observed, dict) or any(observed.get(key) != value for key, value in expected.items()):
            return False
        if (observed.get("operation") != "practice.receipt"
                or observed.get("source") != "successful_cua_repl_js_result"):
            return False
        canonical = json.dumps(expected, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        calls = report.get("mcp_calls", [])
        successful_call = isinstance(calls, list) and any(
            isinstance(call, dict) and call.get("server") == "cua_repl"
            and call.get("tool") == "js" and call.get("status") == "completed" for call in calls)
        return (successful_call and report.get("unapproved_action_events") == []
                and observed.get("result_sha256") == digest(canonical)
                and isinstance(receipt_id, str) and len(receipt_id) == 32
                and all(character in "0123456789abcdefABCDEF" for character in receipt_id))

    reports = sorted(folder.glob("practice-*.report.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    for path in reports:
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        age = observation(path)
        if not isinstance(report, dict):
            continue
        final = report.get("final") or {}
        if not isinstance(final, dict):
            final = {}
        receipt_id = final.get("receipt_id", "")
        receipt = {}
        if isinstance(receipt_id, str) and receipt_id.isalnum():
            try:
                receipt = json.loads((root / "data" / "practice-receipts" / f"{receipt_id}.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
        fixture_path = Path(root) / "tests" / "fixtures" / "fictional-test-resume.txt"
        if not fixture_path.is_file():
            fixture_path = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "fictional-test-resume.txt"
        try:
            expected_text = fixture_path.read_text(encoding="utf-8")
        except OSError:
            expected_text = ""
        matching_receipt = all(receipt.get(key) == value for key, value in {
            "receipt_id": receipt_id, "test_only": True, "name": "Test Candidate",
            "email": "candidate@example.test", "sponsorship": "yes",
            "filename": "fictional-test-resume.txt", "file_sha256": digest(expected_text) if expected_text else None}.items())
        if (final.get("status") == "confirmed" and isinstance(receipt_id, str) and receipt_id.isalnum()
                and final.get("page_url") == "http://127.0.0.1:8765/practice"
                and matching_receipt and practice_observation_valid(report, receipt_id)):
            historical_success = True
            if inventory_valid and not available:
                state, detail = "browser_disconnected", "The latest structured browser inventory shows no connected Chrome browser. A previous local-practice result is historical and does not permit an employer form to open."
            elif not age["fresh"]:
                state, detail = "local_test_passed_stale", f"Local browser fill, upload and receipt were verified on {age['observed_at']}; refresh the practice check before relying on current browser access. Employer workflows remain untested."
            elif not inventory_valid:
                state, detail = "browser_inventory_unverified", "The latest CLI probe lacks a structured browser inventory from successful CUA result content. Current browser availability is unverified; employer workflows remain gated."
            elif not inventory_age["fresh"]:
                state, detail = "browser_inventory_stale", "The local practice succeeded recently, but current Chrome inventory is stale or unavailable. Refresh the browser check before starting an employer form."
            elif not available:
                state, detail = "browser_disconnected", "The local practice succeeded recently, but the latest browser inventory shows no connected Chrome browser. Reconnect before starting an employer form."
            else:
                state, detail = "local_test_passed", "Local browser fill, upload and receipt were verified recently. Employer workflows remain untested."
        elif final.get("status") == "prepared":
            historical_success = False
            if inventory_valid and not available:
                state, detail = "browser_disconnected", "The latest structured browser inventory shows no connected Chrome browser. Employer workflows remain gated."
            elif not inventory_valid:
                state, detail = "browser_inventory_unverified", "The latest CLI probe lacks a structured browser inventory from successful CUA result content. Current browser availability is unverified; employer workflows remain gated."
            elif age["fresh"]:
                state, detail = "form_prepared", "Local fields were verified. Upload and receipt test pending."
            else:
                state, detail = "form_prepared_stale", f"Local fields were verified on {age['observed_at']}; upload and receipt test remains pending."
        else:
            historical_success = False
            failure = json.dumps(report.get("mcp_calls", []))
            if inventory_valid and not available:
                state, detail = "browser_disconnected", "The latest structured browser inventory shows no connected Chrome browser. The older practice failure is historical and cannot determine current browser permissions."
            elif not inventory_valid:
                state, detail = "browser_inventory_unverified", "The latest CLI probe lacks a structured browser inventory from successful CUA result content. Current browser availability is unverified; employer workflows remain gated."
            elif "saved user permission" in failure or "user denied permission" in failure:
                if age["fresh"]:
                    state, detail = "site_permission_required", f"A saved website permission blocked the local practice test on {age['observed_at']}."
                else:
                    state, detail = "site_permission_blocked_stale", f"A saved website permission blocked the local practice test on {age['observed_at']}; this is historical and does not establish current access."
            else:
                state = "test_failed" if age["fresh"] else "test_failed_stale"
                detail = final.get("detail", "Browser control test did not complete.")
                if not age["fresh"]:
                    detail = f"The browser test failed on {age['observed_at']}: {detail}"
        return {"inventory_available": available, "inventory_fresh": inventory_fresh,
                "inventory_observed_at": inventory_age["observed_at"], "state": state, "detail": detail,
                "verified": historical_success and age["fresh"] and inventory_fresh and available,
                "historical_success": historical_success, "practice_fresh": age["fresh"],
                "current_control_available": inventory_fresh and available,
                "inventory_verified": inventory_valid, "browser_names": browser_names,
                "tab_count": tab_count,
                "observed_at": age["observed_at"], "fresh": age["fresh"],
                "report": str(path.relative_to(root))}
    state = ("browser_inventory_unverified" if inventory_age["fresh"] and not inventory_valid else
             "control_unverified" if available and inventory_fresh else
             "inventory_stale" if inventory_valid and not inventory_age["fresh"] else
             "browser_disconnected" if inventory_valid else "unavailable")
    detail = ("The latest CLI probe lacks a structured browser inventory from successful CUA result content." if inventory_age["fresh"] and not inventory_valid else
              "Chrome is visible, but no recent local practice test proves browser control." if available and inventory_fresh else
              f"Chrome was visible on {inventory_age['observed_at']}, but that inventory is stale and browser control is unverified." if available else
              "The latest structured browser inventory shows no connected Chrome browser." if inventory_valid else
              "Chrome inventory and local practice control are unavailable.")
    return {"inventory_available": available, "inventory_fresh": inventory_fresh,
            "inventory_observed_at": inventory_age["observed_at"], "state": state,
            "verified": False, "historical_success": False, "fresh": False, "observed_at": None,
            "inventory_verified": inventory_valid, "browser_names": browser_names, "tab_count": tab_count,
            "current_control_available": inventory_fresh and available,
            "detail": detail}

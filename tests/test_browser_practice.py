import json
from pathlib import Path

from tools.browser_practice import (
    _approval_matches,
    _classify_file_input_value,
    _file_selection_prompt,
    _phase_timeout,
    _permission_note_required,
    _receipt_confirmation_prompt,
    _submit_prompt,
    _verify_prompt,
)
from tools.integration_probe import _compact_mcp_failure


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "fictional-test-resume.txt"


def _approval(**changes):
    value = {
        "status": "approved",
        "action": "local_practice_upload_and_submit",
        "destination": "http://127.0.0.1:8765/practice",
        "file": str(FIXTURE),
        "tab_id": None,
    }
    value.update(changes)
    return value


def test_saved_exact_practice_approval_can_resume_only_supplied_tab():
    assert _approval_matches(_approval(), expected_file=FIXTURE, tab_id="owned-757318735")
    assert not _approval_matches(_approval(tab_id="different-tab"), expected_file=FIXTURE,
                                 tab_id="owned-757318735")
    assert not _approval_matches(_approval(destination="https://example.test/"), expected_file=FIXTURE,
                                 tab_id="owned-757318735")
    assert not _approval_matches(_approval(file=str(ROOT / "other.txt")), expected_file=FIXTURE,
                                 tab_id="owned-757318735")
    assert not _approval_matches(_approval(action="employer_submission"), expected_file=FIXTURE,
                                 tab_id="owned-757318735")


def test_resume_prompt_uses_fresh_chrome_id_and_rich_same_tab_surface():
    prompt = _submit_prompt("757318735", FIXTURE)
    assert "await cua.getState()" in prompt
    assert "cua.getBrowser({id: <current Chrome ID>})" in prompt
    assert "tabs.get('757318735')" in prompt
    assert "do not create a replacement tab" in prompt
    assert "waitForEvent('filechooser')" in prompt
    assert "chooser.setFiles([" in prompt
    assert str(FIXTURE) in prompt
    assert "DOM-backed `value`" in prompt
    assert "do not rely on files.length" in prompt
    assert "do not reselect the file" in prompt
    assert "If the value is empty, and only if it is empty" in prompt
    assert "Keep every JavaScript variable local to the current CUA tool call" in prompt
    assert "never reference an unbound observation variable" in prompt
    assert "Do not throw after emitting observations" in prompt
    assert _classify_file_input_value(
        r"C:\fakepath\fictional-test-resume.txt", FIXTURE) == "matches"
    assert _classify_file_input_value("fictional-test-resume.txt", FIXTURE) == "matches"
    assert _classify_file_input_value("", FIXTURE) == "empty"
    assert _classify_file_input_value(
        r"C:\fakepath\other-resume.txt", FIXTURE) == "ambiguous"
    assert _classify_file_input_value(
        r"C:\fakepath\other-fictional-test-resume.txt", FIXTURE) == "ambiguous"
    assert _classify_file_input_value(None, FIXTURE) == "ambiguous"
    assert "Test Candidate" in prompt
    assert "candidate@example.test" in prompt
    assert "selectOption('yes')" in prompt
    assert "`Yes`" in prompt
    assert "tab.markDeliverable()" in prompt
    assert "tab.markHandoff()" in prompt


def test_receipt_verification_prompt_is_read_only_and_scoped_to_owned_tab():
    prompt = _verify_prompt("owned-757319575")
    assert "await cua.getState();" in prompt
    assert "exact supplied practice tab owned-757319575" in prompt
    assert "cua.getTab(tabId, {browser: browserId})" in prompt
    assert "tab.getAXState()" in prompt
    assert "32-hex ID" in prompt
    assert "Local practice submission received" in prompt
    assert "fictional-test-resume.txt" in prompt
    assert "tab.markHandoff()" in prompt
    assert "Do not mark the tab deliverable in this phase" in prompt
    assert "Do not navigate" in prompt
    assert "Do not" in prompt and "upload files" in prompt
    for modifying_api in ("locator.fill(", "filechooser", "setFiles(", "click Save", "createBrowserTab"):
        assert modifying_api not in prompt


def test_receipt_confirmation_waits_for_async_dom_without_resubmitting():
    prompt = _receipt_confirmation_prompt()
    assert "Click Save local test receipt exactly once" in prompt
    assert "never click Save a second time" in prompt
    assert "at most 30 seconds" in prompt
    assert "up to 30 observations" in prompt
    assert "#practice-confirmation h2" in prompt
    assert "#receipt-id" in prompt and "#receipt-detail" in prompt
    assert "Parse the filename from the JSON currently rendered" in prompt
    assert "nodeRepl.write" in prompt
    assert "tab.markHandoff()" in prompt
    assert "Do not call `tab.markDeliverable()`" in prompt


def test_practice_timeout_is_bounded_and_explicitly_allows_max_regression():
    assert _phase_timeout("complete") == 180
    assert _phase_timeout("complete", 300) == 300
    assert _phase_timeout("submit", 120) == 120
    assert _phase_timeout("verify") == 120
    import pytest
    with pytest.raises(ValueError, match="capped at 120"):
        _phase_timeout("verify", 300)
    with pytest.raises(ValueError, match="120, 180, or 300"):
        _phase_timeout("complete", 240)
    assert _permission_note_required("prepare") is True
    assert _permission_note_required("complete") is False


def test_failed_mcp_report_keeps_text_but_drops_image_and_tab_metadata():
    item = {
        "status": "failed",
        "result": {
            "content": [
                {"type": "text", "text": "Saved user permission blocks this action.\nBrowser tab: 757318735, Title: private title"},
                {"type": "image", "data": "A" * 1000},
            ],
            "screenshot": "B" * 1000,
        },
    }
    detail = _compact_mcp_failure(item)
    assert detail == {"type": None, "message": "Saved user permission blocks this action."}
    assert "private title" not in repr(detail)
    assert "AAAA" not in repr(detail)


def test_practice_dom_observation_uses_only_successful_cua_text_and_is_compact():
    from tools.integration_probe import _practice_dom_observation

    receipt_id = "a" * 32
    visible = (
        "URL http://127.0.0.1:8765/practice\nApplicant name: Test Candidate\n"
        "Email control: [value masked]\nSponsorship selection: [value masked]\n"
        "Selected file: fictional-test-resume.txt\nLocal practice submission received\nReceipt: "
        + receipt_id + '\n{"name_matches":true,"email_matches":false,"email_nonempty":true,'
        '"sponsorship_matches":true}'
    )
    successful_event = {"type": "item.completed", "item": {"type": "mcp_tool_call", "server": "cua_repl",
                        "tool": "js", "status": "completed", "result": {"content": [
                            {"type": "text", "text": visible}, {"type": "image", "data": "X" * 1000}]}}}
    failed_event = {"type": "item.completed", "item": {"type": "mcp_tool_call", "server": "cua_repl",
                    "tool": "js", "status": "failed", "result": {"content": [
                        {"type": "text", "text": "FAILED ONLY: candidate@example.test " + "b" * 32}]}}}
    observation = _practice_dom_observation([failed_event, successful_event], {"receipt_id": receipt_id})
    assert observation["source"] == "successful_cua_repl_js_result"
    assert set(observation["excerpts"]) == {
        "page_url", "name", "name_match", "email_match", "email_nonempty", "sponsorship_match",
        "filename", "receipt_heading", "receipt_id"}
    assert "X" * 100 not in json.dumps(observation)
    assert "candidate@example.test" not in json.dumps(observation)
    assert "FAILED ONLY" not in json.dumps(observation)
    assert len(json.dumps(observation)) < 2500
    assert _practice_dom_observation([failed_event], {"receipt_id": receipt_id}) is None


def test_resumed_tab_presence_is_derived_from_actual_getstate_result():
    from tools.integration_probe import _expected_tab_present

    state = {"apps": [], "browsers": [{"id": "chrome-id", "name": "Chrome",
             "tabs": [{"id": "owned-tab"}, {"id": "other-tab"}]}]}
    event = {"type": "item.completed", "item": {"type": "mcp_tool_call", "server": "cua_repl",
             "tool": "js", "status": "completed", "result": {
                 "content": [{"type": "text", "text": json.dumps(state)}]}}}
    assert _expected_tab_present([event], "owned-tab") is True
    assert _expected_tab_present([event], "missing-tab") is False
    assert _expected_tab_present([{**event, "item": {**event["item"], "status": "failed"}}], "owned-tab") is None


def test_local_practice_prompt_uses_the_form_option_value():
    prompt_source = (ROOT / "tools" / "browser_practice.py").read_text(encoding="utf-8")
    form_markup = (ROOT / "app" / "static" / "practice.html").read_text(encoding="utf-8")
    assert '<option value="yes">Yes</option>' in form_markup
    assert prompt_source.count("selectOption('yes')") >= 2
    assert "Call tab.markDeliverable()" not in prompt_source
    assert "tab.markHandoff()" in prompt_source
    new_tab_file_prompt = _file_selection_prompt(FIXTURE, allow_existing_match=False)
    assert "This is a newly created tab, so stop if the file input is nonempty" in new_tab_file_prompt
    assert "chooser.setFiles([" in new_tab_file_prompt
    assert repr(str(FIXTURE)) in new_tab_file_prompt
    assert "basename exactly matches `fictional-test-resume.txt`" in new_tab_file_prompt

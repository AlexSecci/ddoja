"""Bounded, read-only Codex subscription integration checks.

No shell-built commands, API credentials, employer actions, or browser writes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.cli_paths import codex_invocation_env, resolve_codex_cli

MODEL_SCHEMA = {
    "type": "object",
    "properties": {
        "probe_id": {"type": "string"},
        "decisions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "job_id": {"type": "string"},
                    "decision": {"type": "string", "enum": ["approve", "discard", "ask"]},
                    "reason": {"type": "string"},
                },
                "required": ["job_id", "decision", "reason"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["probe_id", "decisions"],
    "additionalProperties": False,
}

BROWSER_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["connected", "unavailable", "failed"]},
        "tool_called": {"type": "string"},
        "browser_names": {"type": "array", "items": {"type": "string"}},
        "tab_count": {"type": "integer"},
        "detail": {"type": "string"},
    },
    "required": ["status", "tool_called", "browser_names", "tab_count", "detail"],
    "additionalProperties": False,
}


def _state_from_result(value):
    """Find the actual returned getState object, never examples in prose/docs."""
    if isinstance(value, dict):
        if isinstance(value.get("apps"), list) and isinstance(value.get("browsers"), list):
            return value
        structured = value.get("structuredContent")
        if isinstance(structured, dict):
            state = _state_from_result(structured)
            if state is not None:
                return state
        content = value.get("content")
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "text":
                    continue
                state = _state_from_result(block.get("text"))
                if state is not None:
                    return state
    elif isinstance(value, list):
        for block in value:
            if isinstance(block, dict) and block.get("type") == "text":
                state = _state_from_result(block.get("text"))
                if state is not None:
                    return state
    elif isinstance(value, str):
        try:
            decoded = json.loads(value.strip())
        except (TypeError, json.JSONDecodeError):
            return None
        if isinstance(decoded, dict) and isinstance(decoded.get("apps"), list) \
                and isinstance(decoded.get("browsers"), list):
            return decoded
    return None


def _inventory_from_result(result):
    """Normalize the documented `{apps, browsers:[{name,tabs}]}` result shape."""
    state = _state_from_result(result)
    if state is None:
        return None
    browsers = state["browsers"]
    if not all(isinstance(browser, dict) and isinstance(browser.get("name"), str)
               and isinstance(browser.get("tabs"), list) for browser in browsers):
        return None
    names = [browser["name"] for browser in browsers]
    count = sum(len(browser["tabs"]) for browser in browsers)
    normalized = {"browser_names": sorted(set(names)), "tab_count": count}
    canonical = json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return {"operation": "cua.getState", "source": "successful_cua_repl_js_result",
            **normalized, "result_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest()}


def _browser_inventory_observation(events):
    for event in events:
        item = event.get("item", {})
        if (event.get("type") == "item.completed" and item.get("type") == "mcp_tool_call"
                and item.get("server") == "cua_repl" and item.get("tool") == "js"
                and item.get("status") == "completed"):
            observation = _inventory_from_result(item.get("result"))
            if observation is not None:
                return observation
    return None


def _successful_cua_text_blocks(events):
    values = []
    for event in events:
        item = event.get("item", {})
        if (event.get("type") == "item.completed" and item.get("type") == "mcp_tool_call"
                and item.get("server") == "cua_repl" and item.get("tool") == "js"
                and item.get("status") == "completed"):
            result = item.get("result")
            if isinstance(result, dict):
                for block in result.get("content", []):
                    if isinstance(block, dict) and block.get("type") == "text" \
                            and isinstance(block.get("text"), str):
                        values.append(block["text"])
    return values


def _successful_cua_result_text(events):
    return "\n".join(_successful_cua_text_blocks(events))


def _excerpt_for_marker(blocks, marker, *, regex=False):
    for block in blocks:
        match = re.search(marker if regex else re.escape(marker), block, flags=re.IGNORECASE)
        if not match:
            continue
        start = max(0, match.start() - 65)
        end = min(len(block), match.end() + 85)
        excerpt = " ".join(block[start:end].split())
        excerpt = re.sub(r"data:[^\s]+", "[image data omitted]", excerpt)
        excerpt = re.sub(r"[A-Za-z0-9+/]{200,}={0,2}", "[binary data omitted]", excerpt)
        return excerpt[:220]
    return None


def _practice_dom_observation(events, final):
    """Retain small field/receipt excerpts from successful CUA text results only."""
    blocks = _successful_cua_text_blocks(events)
    receipt_id = final.get("receipt_id", "") if isinstance(final, dict) else ""
    markers = {
        "page_url": "http://127.0.0.1:8765/practice",
        "name": "Test Candidate",
        "name_match": r'"name_matches"\s*:\s*true',
        "email_match": r'"email_matches"\s*:\s*(?:true|false)',
        "email_nonempty": r'"email_nonempty"\s*:\s*(?:true|false)',
        "sponsorship_match": r'"sponsorship_matches"\s*:\s*true',
        "filename": "fictional-test-resume.txt",
        "receipt_heading": "Local practice submission received",
        "receipt_id": receipt_id,
    }
    excerpts = {}
    for key, marker in markers.items():
        if isinstance(marker, str) and marker:
            excerpt = _excerpt_for_marker(blocks, marker,
                                          regex=key in {"name_match", "email_match", "email_nonempty",
                                                        "sponsorship_match"})
            if excerpt:
                excerpts[key] = excerpt
    if not excerpts:
        return None
    normalized = {"source": "successful_cua_repl_js_result", "excerpts": excerpts}
    canonical = json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return {**normalized, "result_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest()}


def _expected_tab_present(events, expected_tab_id):
    """Check a resumed owned tab against the actual getState result, not the model summary."""
    if not expected_tab_id:
        return None
    for event in events:
        item = event.get("item", {})
        if (event.get("type") != "item.completed" or item.get("type") != "mcp_tool_call"
                or item.get("server") != "cua_repl" or item.get("tool") != "js"
                or item.get("status") != "completed"):
            continue
        state = _state_from_result(item.get("result"))
        if state is None:
            continue
        tab_ids = []
        for browser in state.get("browsers", []):
            if not isinstance(browser, dict) or not isinstance(browser.get("tabs"), list):
                return None
            for tab in browser["tabs"]:
                if isinstance(tab, dict) and tab.get("id") is not None:
                    tab_ids.append(str(tab["id"]))
        return str(expected_tab_id) in tab_ids
    return None


def _practice_receipt_observation(events, final):
    """Bind the practice success summary to visible text from this CUA invocation."""
    if not isinstance(final, dict) or final.get("status") != "confirmed":
        return None
    receipt_id = final.get("receipt_id", "")
    page_url = final.get("page_url", "")
    if not isinstance(receipt_id, str) or not re.fullmatch(r"[a-fA-F0-9]{32}", receipt_id):
        return None
    if page_url != "http://127.0.0.1:8765/practice":
        return None
    observed = _successful_cua_result_text(events)
    required = [page_url, "Local practice submission received", receipt_id, "fictional-test-resume.txt"]
    if any(value.casefold() not in observed.casefold() for value in required):
        return None
    normalized = {"page_url": page_url, "heading": "Local practice submission received",
                  "receipt_id": receipt_id, "filename": "fictional-test-resume.txt"}
    canonical = json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return {"operation": "practice.receipt", "source": "successful_cua_repl_js_result", **normalized,
            "result_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest()}


def _unapproved_action_events(events):
    safe_item_types = {"agent_message", "reasoning", "user_message", "mcp_tool_call"}
    action_item_types = {"command_execution", "file_change", "file_read", "file_search", "web_search",
                         "image_generation", "computer_call", "shell_command", "browser_action"}
    rejected = []
    for event in events:
        item = event.get("item", {})
        item_type = item.get("type") if isinstance(item, dict) else None
        event_type = event.get("type", "")
        if item_type == "mcp_tool_call":
            if item.get("server") != "cua_repl" or item.get("tool") != "js":
                rejected.append(f"mcp:{item.get('server', '')}.{item.get('tool', '')}")
        elif item_type in action_item_types or event_type in action_item_types:
            rejected.append(str(item_type or event_type))
        elif item_type is not None and item_type not in safe_item_types:
            rejected.append(str(item_type))
    return sorted(set(rejected))


def _compact_mcp_failure(item):
    """Keep useful failure text while discarding screenshots and browser metadata."""
    result = item.get("result")
    message = item.get("error")
    error_type = item.get("error_type") or item.get("code")
    if isinstance(result, dict):
        error_type = error_type or result.get("error_type") or result.get("code")
        if not isinstance(message, str):
            message = result.get("error") or result.get("message")
        if not isinstance(message, str):
            for block in result.get("content", []):
                if isinstance(block, dict) and block.get("type") == "text" \
                        and isinstance(block.get("text"), str):
                    message = block["text"]
                    break
    if not isinstance(message, str):
        message = "The tool call failed; non-text result details were omitted."
    # Browser tool failures may attach tab titles, IDs, URLs, or image data after
    # their diagnostic. Keep the actionable error only.
    message = re.split(r"\n\s*Browser tab:", message, maxsplit=1, flags=re.IGNORECASE)[0]
    message = re.sub(r"data:[^\s]+", "[image data omitted]", message)
    message = re.sub(r"[A-Za-z0-9+/]{300,}={0,2}", "[binary data omitted]", message)
    message = " ".join(message.split())[:400]
    return {"type": error_type if isinstance(error_type, str) else None, "message": message}


def run_probe(codex: str, model: str, browser: bool, timeout: int = 150,
              prompt_override=None, schema_override=None, stem_override=None,
              persistent=False, resume_session=None, reasoning_effort="medium",
              expected_tab_id=None, invocation_id=None) -> dict:
    output = ROOT / "data" / "integration"
    output.mkdir(parents=True, exist_ok=True)
    stem = stem_override or ("browser" if browser else model.replace(".", "-"))
    schema_path = output / f"{stem}.schema.json"
    final_path = output / f"{stem}.response.json"
    schema_path.write_text(json.dumps(schema_override or (BROWSER_SCHEMA if browser else MODEL_SCHEMA)), encoding="utf-8")
    if browser:
        prompt = (
            "Perform one read-only browser integration check. Do not navigate, create/close tabs, "
            "fill any fields, access accounts, send anything, or modify files. "
            "If the cua_repl tool is available, initialize it with exactly await cua.getState(); "
            "as the first and only API call in that invocation. Follow its returned instructions. "
            "Report only whether it succeeded, browser names and the total visible tab count. "
            "Do not report tab titles, URLs or personal data. If the tool is absent or fails, "
            "report unavailable/failed and a concise exact error without credentials. "
            "Do not search the filesystem, inspect environment variables or improvise another browser interface."
        )
    else:
        prompt = (
            "This is a synthetic queue integration test. No tool calls or file changes. "
            "Return probe_id subscription-smoke-v1 and decisions for exactly these three jobs. "
            "Applicant: US relocation, full-time onsite/hybrid, first H1B sponsorship needed. "
            "C++ is confirmed yes; React is unknown. "
            "A: Austin, full-time onsite, C++ required, explicitly offers sponsorship. "
            "B: Italy, full-time onsite, C++ required. "
            "C: Austin, full-time hybrid, React required, sponsorship not specified. "
            "Use approve/discard/ask. Unknown required skills must be asked; unknown sponsorship "
            "must not be described as available. Include short reasons."
        )
    if prompt_override is not None:
        prompt = prompt_override
    args = [
        codex, "exec", "--json", "--skip-git-repo-check",
        "--sandbox", "read-only", "--color", "never", "--model", model,
        "-c", 'model_provider="openai"',
        "-c", 'forced_login_method="chatgpt"',
        "-c", f'model_reasoning_effort="{reasoning_effort}"',
        "--output-schema", str(schema_path),
        "--output-last-message", str(final_path),
    ]
    if not persistent and not resume_session:
        args.append("--ephemeral")
    if resume_session:
        args.extend(["resume", resume_session])
    args.append("-")
    env = os.environ.copy()
    for key in ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN"):
        env.pop(key, None)
    env = codex_invocation_env(ROOT, invocation_id or uuid.uuid4().hex, env)
    # Codex reads its existing managed sign-in; authentication files are never opened here.
    authentication = subprocess.run([codex, "login", "status"], env=env, capture_output=True,
                                    text=True, encoding="utf-8", errors="replace", timeout=20)
    auth_text = authentication.stdout + authentication.stderr
    if authentication.returncode or "ChatGPT" not in auth_text:
        raise RuntimeError("Existing ChatGPT sign-in is required; no API-key fallback is allowed.")
    started = time.monotonic()
    process = subprocess.Popen(args, cwd=ROOT, env=env, stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, encoding="utf-8", errors="replace")
    timed_out = False
    try:
        stdout, stderr = process.communicate(prompt, timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           capture_output=True, timeout=15)
        else:
            process.kill()
        stdout, stderr = process.communicate(timeout=15)
    events = []
    for line in stdout.splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    final = None
    # Parse the current invocation's completed message, not a potentially stale output file.
    for event in events:
        item = event.get("item", {})
        if event.get("type") == "item.completed" and item.get("type") == "agent_message":
            try:
                final = json.loads(item.get("text", ""))
            except json.JSONDecodeError:
                continue
    report = {
        "requested_model": model,
        "authentication": "existing_chatgpt_sign_in",
        "browser_probe": browser,
        "returncode": process.returncode,
        "timed_out": timed_out,
        "elapsed_seconds": round(time.monotonic() - started, 2),
        "session_id": next((event.get("thread_id") for event in events
                            if event.get("type") == "thread.started"), resume_session),
        "event_types": [event.get("type") for event in events],
        "usage": [event.get("usage") for event in events if event.get("type") == "turn.completed"],
        "errors": [event.get("message", event.get("error")) for event in events
                   if event.get("type") in ("error", "turn.failed")],
        "mcp_calls": [{"server": event["item"].get("server"),
                       "tool": event["item"].get("tool"),
                       "status": event["item"].get("status"),
                       "failure_detail": _compact_mcp_failure(event["item"])
                       if event["item"].get("status") == "failed" else None}
                      for event in events if event.get("type") == "item.completed"
                      and event.get("item", {}).get("type") == "mcp_tool_call"],
        "unapproved_action_events": _unapproved_action_events(events),
        "browser_inventory_observation": (_browser_inventory_observation(events)
                                           if browser and not _unapproved_action_events(events) else None),
        "expected_tab_present": (_expected_tab_present(events, expected_tab_id)
                                 if browser and expected_tab_id and not _unapproved_action_events(events) else None),
        "browser_practice_dom_observation": (_practice_dom_observation(events, final)
                                             if browser and not _unapproved_action_events(events) else None),
        "browser_practice_observation": (_practice_receipt_observation(events, final)
                                         if browser and not _unapproved_action_events(events) else None),
        "final": final,
        "stderr_tail": stderr[-3000:],
    }
    (output / f"{stem}.report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="gpt-6-luna")
    parser.add_argument("--browser", action="store_true")
    parser.add_argument("--codex", default=resolve_codex_cli())
    parser.add_argument("--timeout", type=int, default=150)
    parser.add_argument("--reasoning-effort", choices=("low", "medium", "high", "xhigh", "max"), default="medium")
    options = parser.parse_args()
    if not options.codex:
        parser.error("Codex executable not found.")
    print(json.dumps(run_probe(options.codex, options.model, options.browser, options.timeout,
                               reasoning_effort=options.reasoning_effort), indent=2))

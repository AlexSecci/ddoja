"""Bounded, read-only Codex subscription integration checks.

No shell-built commands, API credentials, employer actions, or browser writes.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]

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


def run_probe(codex: str, model: str, browser: bool, timeout: int = 150,
              prompt_override=None, schema_override=None, stem_override=None,
              persistent=False, resume_session=None) -> dict:
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
        "-c", 'model_reasoning_effort="medium"',
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
                       "error": event["item"].get("error"),
                       "failure_detail": event["item"].get("result") if event["item"].get("status") == "failed" else None}
                      for event in events if event.get("type") == "item.completed"
                      and event.get("item", {}).get("type") == "mcp_tool_call"],
        "final": final,
        "stderr_tail": stderr[-3000:],
    }
    (output / f"{stem}.report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="gpt-6-luna")
    parser.add_argument("--browser", action="store_true")
    parser.add_argument("--codex", default=shutil.which("codex"))
    parser.add_argument("--timeout", type=int, default=150)
    options = parser.parse_args()
    if not options.codex:
        parser.error("Codex executable not found.")
    print(json.dumps(run_probe(options.codex, options.model, options.browser, options.timeout), indent=2))

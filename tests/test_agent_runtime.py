from __future__ import annotations

import io
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from app import agent_runtime, cli_paths
from tools.agent_mcp import AgentMCPBridge, ROLE_TOOLS, serve


ROOT = Path(__file__).resolve().parents[1]


def _runtime_root(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "prompts").mkdir()
    (tmp_path / "tools").mkdir()
    (tmp_path / "config" / "workers.json").write_text(json.dumps({
        "authentication": "chatgpt",
        "models": {
            "discovery": {"model": "gpt-6-luna", "effort": "medium"},
            "evaluation": {"model": "gpt-6.1-sol", "effort": "high"},
            "submission": {"model": "gpt-6-luna", "effort": "high"},
            "materials": {"model": "gpt-6.1-sol", "effort": "high"},
        },
    }), encoding="utf-8")
    for name in agent_runtime.ROLE_PROMPTS.values():
        (tmp_path / "prompts" / name).write_text("role instructions", encoding="utf-8")
    (tmp_path / "tools" / "agent_mcp.py").write_text("# fixture", encoding="utf-8")
    return tmp_path


def _task(role="discovery", session_id=None):
    worker = {"id": "worker-fixture", "role": role, "pipeline_id": "pipe-fixture",
              "job_id": None, "state": "running", "session_id": session_id,
              "resume_mode": "resume" if session_id else "new", "checkpoint": "coverage"}
    return {"worker": worker, "capability": "capability-fixture-never-in-argv",
            "task_context": {"company": "Fixture Co", "secret_example": "confirmed test value"}}


class _FakeProcess:
    next_session = "session-first"
    instances = []

    def __init__(self, args, **kwargs):
        self.args = args
        self.env = kwargs["env"]
        self.returncode = 0
        self.stdin = io.BytesIO()
        self.stdout = io.BytesIO(self._events())
        self.stderr = io.BytesIO(b"")
        self.instances.append(self)

    def _events(self):
        resumed = "resume" in self.args
        session = self.next_session
        events = []
        if resumed:
            events.append({"type": "thread.resumed", "thread_id": session})
        else:
            events.append({"type": "thread.started", "thread_id": session})
        events.extend([
            {"type": "item.completed", "item": {"type": "mcp_tool_call", "server": "agent_bridge", "tool": "get_context"}},
            {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps({
                "disposition": "completed", "checkpoint": "done", "summary": "Fixture turn completed."})}},
            {"type": "turn.completed", "usage": {"input_tokens": 17, "output_tokens": 3}},
        ])
        return ("\n".join(json.dumps(event) for event in events) + "\n").encode()

    def poll(self):
        return self.returncode

    def wait(self):
        return self.returncode

    def send_signal(self, _signal):
        self.returncode = 1

    def terminate(self):
        self.returncode = 1

    def kill(self):
        self.returncode = 1


def test_agent_bridge_role_scope_and_model_cannot_supply_task_identity():
    calls = []

    def action(worker_id, token, action, payload):
        calls.append((worker_id, token, action, payload))
        return {"ok": True}

    bridge = AgentMCPBridge("worker-1", "short-token", "submission", action)
    tool_definitions = {tool["name"]: tool for tool in bridge.tools()}
    assert "ask_question" in tool_definitions
    assert "request_approval" in {tool["name"] for tool in bridge.tools()}
    assert "begin_final_action" in {tool["name"] for tool in bridge.tools()}
    assert tool_definitions["get_context"]["annotations"]["readOnlyHint"] is True
    assert tool_definitions["lookup_listing"]["annotations"]["readOnlyHint"] is True
    assert "annotations" not in tool_definitions["finish_submission"]
    assert bridge.call("checkpoint", {"checkpoint": "form", "context": {"tab_id": "owned"}}) == {"ok": True}
    assert calls == [("worker-1", "short-token", "checkpoint", {"checkpoint": "form", "context": {"tab_id": "owned"}})]
    with pytest.raises(ValueError, match="not available"):
        bridge.call("record_listing", {"url": "https://jobs.example.test", "title": "x", "jd": "x", "selected": True})
    with pytest.raises(ValueError, match="supplied by the bridge"):
        bridge.call("finish_submission", {"status": "submitted", "detail": "x", "job_id": "other-job"})


def test_role_scopes_include_progress_checkpoints_and_document_question():
    assert {"checkpoint", "request_approval"} <= set(ROLE_TOOLS["discovery"])
    assert {"checkpoint", "request_approval"} <= set(ROLE_TOOLS["evaluator"])
    assert {"ask_question", "finish_submission"} <= set(ROLE_TOOLS["submission"])
    assert {"ask_question", "publish_documents"} <= set(ROLE_TOOLS["document"])
    assert "begin_final_action" in ROLE_TOOLS["submission"]
    assert "ask_question" in ROLE_TOOLS["discovery"]
    assert "request_approval" not in ROLE_TOOLS["document"]


def test_bridge_exposes_scoped_fact_plan_and_capture_contracts():
    discovery = AgentMCPBridge("d", "cap", "discovery", lambda *_: {"ok": True})
    evaluator = AgentMCPBridge("e", "cap", "evaluator", lambda *_: {"ok": True})
    submission = AgentMCPBridge("s", "cap", "submission", lambda *_: {"ok": True})
    document = AgentMCPBridge("m", "cap", "document", lambda *_: {"ok": True})
    assert "capture_listing_batch" in ROLE_TOOLS["discovery"]
    assert {"lookup_workflow_plan", "save_workflow_plan"} <= set(ROLE_TOOLS["discovery"])
    assert {"lookup_workflow_plan", "save_workflow_plan"} <= set(ROLE_TOOLS["evaluator"])
    assert {"lookup_workflow_plan", "save_workflow_plan"} <= set(ROLE_TOOLS["submission"])
    assert not ({"lookup_workflow_plan", "save_workflow_plan", "capture_listing_batch"}
                & set(ROLE_TOOLS["document"]))
    all_defs = {tool["name"]: tool for tool in discovery.tools() + evaluator.tools() + submission.tools()
                + document.tools()}
    assert len(all_defs) == len({tool["name"] for tool in discovery.tools() + evaluator.tools()
                                  + submission.tools() + document.tools()})
    assert "prepared_document_handles" in all_defs["get_context"]["inputSchema"]["properties"]["sections"]["items"]["enum"]
    assert "submission_state" in all_defs["get_context"]["inputSchema"]["properties"]["sections"]["items"]["enum"]
    assert "parent_context" in all_defs["get_context"]["inputSchema"]["properties"]["sections"]["items"]["enum"]
    question_schema = all_defs["ask_question"]["inputSchema"]["properties"]
    assert question_schema["fact_key"]["maxLength"] == 160
    assert question_schema["fact_scope"]["enum"] == ["applicant", "job"]
    pointer_schema = all_defs["capture_listing_batch"]["inputSchema"]["properties"]["listings"]["items"]["properties"]["description_pointer"]
    assert pointer_schema["required"] == ["text_block", "start_marker", "end_marker"]


def test_bridge_requires_fact_scope_and_key_as_a_pair_and_rejects_bad_capture_pointer():
    calls = []
    bridge = AgentMCPBridge("w", "cap", "submission",
                            lambda worker, token, name, payload: calls.append((name, payload)) or {"ok": True})
    with pytest.raises(ValueError, match="both fact_key and fact_scope"):
        bridge.call("ask_question", {"question": "Where are you based?", "fact_key": "location.country"})
    assert bridge.call("ask_question", {"question": "Where are you based?", "fact_key": "location.country",
                                         "fact_scope": "applicant"}) == {"ok": True}
    assert calls[0][1]["fact_key"] == "location.country"
    discovery = AgentMCPBridge("d", "cap", "discovery", lambda *_: {"ok": True})
    with pytest.raises(ValueError, match="text-block index"):
        discovery.call("capture_listing_batch", {"listings": [{"url": "https://jobs.example.test/1",
                "title": "Fixture", "selected": True, "description_pointer": {
                    "text_block": True, "start_marker": "START", "end_marker": "END"}}]})


def test_runtime_normalizes_backend_launch_envelope_and_preserves_scoped_context():
    launch = {"capability": "short-lived", "task": {
        "worker": {"id": "w1", "role": "evaluator", "session_id": None},
        "pipeline": {"company_name": "Fixture"}, "job": {"jd": "Exact JD"},
        "applicant": {"confirmed_knowledge": ["fact"]}, "inbox_history": [{"response": "answer"}],
    }}
    normalized = agent_runtime.CodexAgentRuntime._normalize_launch_context(launch)
    assert normalized["worker"]["role"] == "evaluator"
    assert normalized["capability"] == "short-lived"
    assert normalized["task_context"] == {
        "pipeline": launch["task"]["pipeline"], "job": launch["task"]["job"],
        "applicant": launch["task"]["applicant"], "inbox_history": launch["task"]["inbox_history"],
    }


def test_document_publish_returns_preview_images_separate_from_structured_payload(tmp_path):
    preview = tmp_path / "resume.png"
    preview.write_bytes(b"png-fixture")
    seen = []

    def action(worker, token, name, payload):
        seen.append((worker, token, name, payload))
        return {"package_id": "pkg-1", "artifacts": [{"kind": "resume", "sha256": "a" * 64}]}

    bridge = AgentMCPBridge("doc-1", "doc-token", "document", action,
                            document_renderer=lambda published: [{"kind": "resume", "filename": "resume.pdf",
                                                                  "pages": 1, "text_matches": True,
                                                                  "sha256": "b" * 64, "previews": [str(preview)]}])
    value, images = bridge.invoke("publish_documents", {"artifacts": [{"kind": "resume", "markdown": "# Resume"}]})
    assert seen[0][2] == "publish_documents"
    assert value["rendered_previews"] == [{"kind": "resume", "filename": "resume.pdf", "pages": 1,
                                            "text_matches": True, "sha256": "b" * 64}]
    assert len(images) == 1 and images[0]["mimeType"] == "image/png"
    assert images[0]["data"] == "cG5nLWZpeHR1cmU="


def test_stdio_smoke_server_implements_initialize_list_and_tool_call(tmp_path):
    bridge_path = ROOT / "tools" / "agent_mcp.py"
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "ping", "arguments": {}}},
    ]
    env = os.environ.copy()
    env["AGENT_BRIDGE_SMOKE"] = "1"
    result = subprocess.run([sys.executable, str(bridge_path)], input="\n".join(map(json.dumps, messages)) + "\n",
                            text=True, capture_output=True, env=env, timeout=10)
    assert result.returncode == 0
    replies = [json.loads(line) for line in result.stdout.splitlines()]
    assert [reply["id"] for reply in replies] == [1, 2, 3]
    assert replies[0]["result"]["serverInfo"]["name"] == "job-application-agent-bridge"
    assert replies[1]["result"]["tools"][0]["name"] == "ping"
    assert replies[1]["result"]["tools"][0]["annotations"] == {"readOnlyHint": True}
    assert replies[2]["result"]["structuredContent"] == {"ok": True, "fixture": "agent-mcp-smoke", "transport": "stdio"}


def test_runtime_runs_then_resumes_same_saved_codex_session_without_capability_in_args(tmp_path, monkeypatch):
    root = _runtime_root(tmp_path)
    settings = tmp_path / "codex-home"
    settings.mkdir()
    (settings / "config.toml").write_text(
        '[plugins."unified-computer-use@openai-bundled"]\nenabled = true\n'
        '[plugins."documents@openai-primary-runtime"]\nenabled = true\n'
        '[mcp_servers.node_repl]\nenabled = true\n', encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(settings))
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-cross-process")
    monkeypatch.setenv("CODEX_API_KEY", "must-not-cross-process")
    monkeypatch.setenv("TEMP", "parent-temp")
    monkeypatch.setenv("TMP", "parent-tmp")
    monkeypatch.setattr(cli_paths.platform, "system", lambda: "Windows")
    monkeypatch.setattr(agent_runtime.subprocess, "Popen", _FakeProcess)
    _FakeProcess.instances.clear()
    _FakeProcess.next_session = "session-first"
    runtime = agent_runtime.CodexAgentRuntime(root, "codex-fixture", login_checker=lambda _codex, _env: True)
    observed = []
    first = runtime.run(_task("discovery"), observed.append, lambda: False)
    assert first["disposition"] == "completed"
    assert first["session_id"] == "session-first"
    first_args = _FakeProcess.instances[-1].args
    first_env = _FakeProcess.instances[-1].env
    first_temp = Path(first_env["TEMP"])
    assert first_temp == Path(first_env["TMP"])
    assert first_temp.is_dir() and first_temp.is_relative_to(root.resolve())
    assert os.environ["TEMP"] == "parent-temp" and os.environ["TMP"] == "parent-tmp"
    assert "capability-fixture-never-in-argv" not in first_args
    assert "AGENT_CAPABILITY" in " ".join(first_args)
    assert "capability-fixture-never-in-argv" not in " ".join(first_args)
    assert first_env["AGENT_CAPABILITY"] == "capability-fixture-never-in-argv"
    assert "OPENAI_API_KEY" not in first_env and "CODEX_API_KEY" not in first_env
    import tomllib
    plugin_arg = next(arg for arg in first_args if arg.startswith("plugins="))
    plugins = tomllib.loads(plugin_arg)["plugins"]
    assert plugins["unified-computer-use@openai-bundled"]["enabled"] is True
    assert plugins["documents@openai-primary-runtime"]["enabled"] is False
    assert "mcp_servers.node_repl.enabled=false" in first_args
    assert not any(argument.startswith("mcp_servers.cua_repl.") for argument in first_args)
    assert 'mcp_servers.agent_bridge.env_vars=["AGENT_ROOT", "AGENT_WORKER_ID", "AGENT_CAPABILITY", "AGENT_ROLE"]' in first_args
    assert 'mcp_servers.agent_bridge.tools.get_context.approval_mode="approve"' in first_args
    assert 'mcp_servers.agent_bridge.tools.record_listing.approval_mode="approve"' in first_args
    assert not any("cua_repl" in argument and "approval_mode" in argument for argument in first_args)
    assert not any(argument.startswith("approval_policy=") or "default_tools_approval_mode" in argument
                   for argument in first_args)

    second_task = _task("discovery", session_id=first["session_id"])
    second_task["worker"]["checkpoint"] = "after-page-1"
    second = runtime.run(second_task, observed.append, lambda: False)
    assert second["disposition"] == "completed"
    assert second["session_id"] == "session-first"
    second_args = _FakeProcess.instances[-1].args
    second_env = _FakeProcess.instances[-1].env
    assert second_env["TEMP"] == second_env["TMP"]
    assert Path(second_env["TEMP"]).is_dir()
    assert Path(second_env["TEMP"]) != first_temp
    assert os.environ["TEMP"] == "parent-temp" and os.environ["TMP"] == "parent-tmp"
    assert "resume" in second_args and "session-first" in second_args
    assert len([event for event in observed if event["type"] not in {"usage", "timing_summary"}]) == 8
    assert len([event for event in observed if event["type"] == "timing_summary"]) == 2
    assert len([event for event in observed if event["type"] == "usage"]) == 2


def test_runtime_keeps_cua_disabled_for_document_workers_and_enabled_for_browser_workers(tmp_path, monkeypatch):
    root = _runtime_root(tmp_path)
    settings = tmp_path / "codex-home"
    settings.mkdir()
    (settings / "config.toml").write_text(
        '[plugins."unified-computer-use@openai-bundled"]\nenabled = true\n', encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(settings))
    runtime = agent_runtime.CodexAgentRuntime(root, "codex-fixture", login_checker=lambda _codex, _env: True)
    bridge_env = {"AGENT_ROOT": str(root), "AGENT_WORKER_ID": "w", "AGENT_CAPABILITY": "unused", "AGENT_ROLE": "document"}
    schema = root / "schema.json"

    document_args = runtime._args({"id": "w", "role": "document", "session_id": None}, schema, bridge_env)
    discovery_args = runtime._args({"id": "w", "role": "discovery", "session_id": None}, schema, bridge_env)
    assert "mcp_servers.cua_repl.enabled=false" in document_args
    assert 'mcp_servers.cua_repl.command=' + json.dumps(sys.executable) in document_args
    assert "mcp_servers.cua_repl.args=[]" in document_args
    assert not any(argument.startswith("mcp_servers.cua_repl.") for argument in discovery_args)
    assert 'mcp_servers.agent_bridge.tools.publish_documents.approval_mode="approve"' in document_args
    assert 'mcp_servers.agent_bridge.tools.record_document_renders.approval_mode' not in document_args
    smoke_args = runtime._args(
        {"id": "w", "role": "document", "session_id": None}, schema,
        {**bridge_env, "AGENT_BRIDGE_SMOKE": "1"})
    assert 'mcp_servers.agent_bridge.tools.ping.approval_mode="auto"' in smoke_args
    assert 'mcp_servers.agent_bridge.tools.publish_documents.approval_mode' not in smoke_args


def test_internal_bridge_approval_is_limited_to_each_role_allowlist(tmp_path, monkeypatch):
    root = _runtime_root(tmp_path)
    runtime = agent_runtime.CodexAgentRuntime(root, "codex-fixture", login_checker=lambda _codex, _env: True)
    from tools.agent_mcp import ROLE_TOOLS

    for role, tool_names in ROLE_TOOLS.items():
        args = runtime._args(
            {"id": "worker-fixture", "role": role, "session_id": None},
            root / "schema.json",
            {"AGENT_ROOT": str(root), "AGENT_WORKER_ID": "worker-fixture",
             "AGENT_CAPABILITY": "fixture-secret-not-in-argv", "AGENT_ROLE": role},
        )
        approvals = {argument for argument in args if ".approval_mode=" in argument}
        expected = {f'mcp_servers.agent_bridge.tools.{name}.approval_mode="approve"'
                    for name in tool_names}
        assert approvals == expected
        assert not any("cua_repl" in argument or "default_tools_approval_mode" in argument
                       or argument.startswith("approval_policy=") for argument in approvals)


def test_runtime_stop_before_launch_does_not_spawn_cli(tmp_path, monkeypatch):
    root = _runtime_root(tmp_path)
    runtime = agent_runtime.CodexAgentRuntime(root, "codex-fixture", login_checker=lambda _codex, _env: True)
    monkeypatch.setattr(agent_runtime.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("must not launch"))
    result = runtime.run(_task(), lambda _event: None, lambda: True)
    assert result["disposition"] == "interrupted"
    assert result["checkpoint"] == "coverage"


@pytest.mark.parametrize("error", [
    {"code": "usage_limit_exceeded"},
    {"type": "UsageLimitExceeded"},
    {"codexErrorInfo": {"type": "UsageLimitExceeded"}},
    {"info": {"error_type": "account_usage_limit"}},
])
def test_runtime_classifies_only_explicit_nested_quota_error_codes(error):
    assert agent_runtime._is_quota_error(error)


def test_runtime_does_not_turn_generic_http_429_into_quota_pause():
    assert not agent_runtime._is_quota_error({"status": 429, "message": "too many requests"})
    assert not agent_runtime._is_quota_error({"code": "authentication_error"})


def test_runtime_accepts_only_narrow_provider_message_quota_fallback():
    assert agent_runtime._is_quota_error({"message": "You have reached your usage limit."})
    assert not agent_runtime._is_quota_error({"message": "This page has a quota limit for applicants."})


def test_runtime_accepts_current_subscription_quota_message_with_plan_and_reset_details():
    assert agent_runtime._is_quota_error({
        "message": "You've hit your usage limit. Upgrade to Pro (more usage) or try again at 9:36 PM."
    })
    assert not agent_runtime._is_quota_error({
        "message": "The account hit its usage limit on this employer page; please upgrade."
    })


def test_runtime_error_extractors_use_typed_provider_events_only():
    assert agent_runtime._error_from_event({"type": "error", "message": "provider error"}) == {"message": "provider error"}
    assert agent_runtime._error_from_event({"type": "item.completed", "item": {
        "type": "agent_message", "text": "usage limit exceeded"}}) is None
    assert agent_runtime._is_missing_session_error({"codexErrorInfo": {"type": "SessionNotFound"}})
    assert not agent_runtime._is_missing_session_error({"message": "session not found in employer form"})


def test_runtime_callback_persistence_failure_is_not_hidden_as_stop(tmp_path, monkeypatch):
    root = _runtime_root(tmp_path)
    runtime = agent_runtime.CodexAgentRuntime(root, "codex-fixture", login_checker=lambda _codex, _env: True)
    monkeypatch.setattr(agent_runtime.subprocess, "Popen", _FakeProcess)
    _FakeProcess.instances.clear()
    result = runtime.run(_task(), lambda _event: (_ for _ in ()).throw(RuntimeError("database down")), lambda: False)
    assert result["disposition"] == "failed"
    assert "durably record" in result["error"]
    assert result['persistence_error_type'] == 'RuntimeError'
    report = json.loads(Path(result['diagnostic_report_path']).read_text())
    assert report['persistence_error_type'] == 'RuntimeError'
    assert 'database down' not in json.dumps(report)


def test_runtime_missing_saved_session_is_explicit_nonterminal(tmp_path, monkeypatch):
    root = _runtime_root(tmp_path)

    class MissingSessionProcess(_FakeProcess):
        def _events(self):
            return (json.dumps({"type": "error", "error": {"codexErrorInfo": {
                "type": "SessionNotFound"}, "message": "Saved session not found"}}) + "\n").encode()

    runtime = agent_runtime.CodexAgentRuntime(root, "codex-fixture", login_checker=lambda _codex, _env: True)
    monkeypatch.setattr(agent_runtime.subprocess, "Popen", MissingSessionProcess)
    result = runtime.run(_task("document", session_id="missing-session"), lambda _event: None, lambda: False)
    assert result["disposition"] == "session_missing"
    assert result["session_id"] == "missing-session"
    assert "explicit safe-resume decision" in result["error"]


def test_runtime_projects_only_completed_cua_js_results_into_evidence():
    actual = {"type": "item.completed", "item": {"type": "mcp_tool_call", "server": "cua_repl",
              "tool": "js", "status": "completed", "result": {"content": [{"type": "text", "text": "actual observation"}],
              "isError": False}}}
    projected = agent_runtime.CodexAgentRuntime._cua_tool_result(actual, "session-1")
    assert projected is not None
    assert projected["type"] == "tool_result" and projected["tool_name"] == "cua_repl.js"
    assert projected["success"] is True and projected["session_id"] == "session-1"
    assert projected["transport_event"] == actual
    assert agent_runtime.CodexAgentRuntime._cua_tool_result(
        {**actual, "type": "item.started"}, "session-1") is None
    failed = {"type": "item.completed", "item": {**actual["item"], "result": {"isError": True}}}
    failed_projection = agent_runtime.CodexAgentRuntime._cua_tool_result(failed, "session-1")
    assert failed_projection is not None and failed_projection["success"] is False
    claimed_only = {"type": "item.completed", "item": {"type": "agent_message", "text": "CUA succeeded"}}
    assert agent_runtime.CodexAgentRuntime._cua_tool_result(claimed_only, "session-1") is None


def test_runtime_detects_cua_action_start_for_last_moment_stop_check():
    event = {"type": "item.started", "item": {"type": "mcp_tool_call", "server": "cua_repl", "tool": "js"}}
    assert agent_runtime.CodexAgentRuntime._is_cua_tool_start(event)
    assert not agent_runtime.CodexAgentRuntime._is_cua_tool_start({**event, "type": "item.completed"})
    assert not agent_runtime.CodexAgentRuntime._is_cua_tool_start({"type": "item.started", "item": {
        "type": "mcp_tool_call", "server": "agent_bridge", "tool": "begin_final_action"}})


def test_runtime_rejects_shell_file_change_web_and_unscoped_mcp_tool_events():
    for item_type in ("commandExecution", "fileChange", "webSearch"):
        event = {"type": "item.started", "item": {"type": item_type, "id": "fixture"}}
        assert agent_runtime.CodexAgentRuntime._tool_access_violation(event, "document")
    assert agent_runtime.CodexAgentRuntime._tool_access_violation({"type": "item.started", "item": {
        "type": "mcp_tool_call", "server": "other_server", "tool": "write"}}, "submission") == "mcp_tool_call"
    assert agent_runtime.CodexAgentRuntime._tool_access_violation({"type": "item.started", "item": {
        "type": "mcp_tool_call", "server": "agent_bridge", "tool": "get_context"}}, "submission") is None
    assert agent_runtime.CodexAgentRuntime._tool_access_violation({"type": "item.started", "item": {
        "type": "mcp_tool_call", "server": "cua_repl", "tool": "js"}}, "evaluator") is None
    assert agent_runtime.CodexAgentRuntime._tool_access_violation({"type": "item.started", "item": {
        "type": "mcp_tool_call", "server": "cua_repl", "tool": "js"}}, "document") == "mcp_tool_call"


def test_runtime_interrupts_and_rejects_actual_shell_tool_event(tmp_path, monkeypatch):
    root = _runtime_root(tmp_path)

    class ShellToolProcess(_FakeProcess):
        def _events(self):
            return ("\n".join(json.dumps(event) for event in [
                {"type": "thread.started", "thread_id": "session-shell"},
                {"type": "item.started", "item": {"type": "commandExecution", "command": ["Get-Content", "private.txt"]}},
                {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps({
                    "disposition": "completed", "checkpoint": "done", "summary": "Done."})}},
                {"type": "turn.completed"},
            ]) + "\n").encode()

    runtime = agent_runtime.CodexAgentRuntime(root, "codex-fixture", login_checker=lambda _codex, _env: True)
    monkeypatch.setattr(agent_runtime.subprocess, "Popen", ShellToolProcess)
    saved = []
    result = runtime.run(_task("document"), saved.append, lambda: False)
    assert result["disposition"] == "failed"
    assert "outside this worker's approved bridge" in result["error"]
    assert {"type": "tool_policy_violation", "tool_type": "commandexecution", "session_id": "session-shell"} in saved
    assert not any("command" in event for event in saved if isinstance(event, dict))


def test_runtime_stderr_diagnostics_are_classified_without_persisting_text():
    assert agent_runtime.CodexAgentRuntime._stderr_diagnostic([]) == "empty"
    assert agent_runtime.CodexAgentRuntime._stderr_diagnostic(
        ["Error: failed to initialize required MCP server agent_bridge"]) == "mcp_startup"
    assert agent_runtime.CodexAgentRuntime._stderr_diagnostic(
        ["You've hit your usage limit. Upgrade to Pro or try again at 9:36 PM."]) == "subscription_quota"
    assert agent_runtime.CodexAgentRuntime._stderr_diagnostic(
        ["Error: authentication required for Codex login"]) == "authentication"
    assert agent_runtime.CodexAgentRuntime._stderr_diagnostic(
        ["employer page says authentication is required"]) == "unclassified_cli_stderr"


def test_runtime_retains_redacted_private_report_for_startup_failure_before_first_event(tmp_path, monkeypatch):
    root = _runtime_root(tmp_path)

    class StartupFailure(_FakeProcess):
        def __init__(self, args, **kwargs):
            super().__init__(args, **kwargs)
            self.returncode = 1
            self.stdout = io.BytesIO(b"")
            self.stderr = io.BytesIO(
                b"Error: failed to initialize required MCP server agent_bridge; "
                b"AGENT_CAPABILITY=capability-fixture-never-in-argv; OPENAI_API_KEY=secret-value\n")

    runtime = agent_runtime.CodexAgentRuntime(root, "codex-fixture", login_checker=lambda _codex, _env: True)
    monkeypatch.setattr(agent_runtime.subprocess, "Popen", StartupFailure)
    result = runtime.run(_task("document"), lambda _event: None, lambda: False)

    assert result["disposition"] == "failed"
    assert result["event_count"] == 0
    report_path = Path(result["diagnostic_report_path"])
    assert report_path.is_file()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["stderr_diagnostic"] == "mcp_startup"
    assert "failed to initialize required MCP server agent_bridge" in report["startup_stderr_excerpt"]
    assert "AGENT_CAPABILITY=[REDACTED]" in report["startup_stderr_excerpt"]
    assert "capability-fixture-never-in-argv" not in report_path.read_text(encoding="utf-8")
    assert "secret-value" not in report_path.read_text(encoding="utf-8")
    assert "argv" not in report and "environment" not in report and "prompt" not in report


def test_runtime_run_normalizes_real_completed_cua_event_before_persist_callback(tmp_path, monkeypatch):
    root = _runtime_root(tmp_path)

    class CUAResultProcess(_FakeProcess):
        def _events(self):
            return ("\n".join(json.dumps(event) for event in [
                {"type": "thread.started", "thread_id": "session-cua"},
                {"type": "item.completed", "item": {"type": "mcp_tool_call", "server": "cua_repl",
                 "tool": "js", "status": "completed", "result": {"content": [{"type": "text", "text": "Observed page"}],
                 "isError": False}}},
                {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps({
                    "disposition": "completed", "checkpoint": "done", "summary": "Done."})}},
                {"type": "turn.completed"},
            ]) + "\n").encode()

    runtime = agent_runtime.CodexAgentRuntime(root, "codex-fixture", login_checker=lambda _codex, _env: True)
    monkeypatch.setattr(agent_runtime.subprocess, "Popen", CUAResultProcess)
    saved = []
    result = runtime.run(_task("evaluator"), saved.append, lambda: False)
    assert result["disposition"] == "completed"
    evidence = next(event for event in saved if event.get("type") == "tool_result")
    assert evidence["tool_name"] == "cua_repl.js"
    assert evidence["success"] is True and evidence["session_id"] == "session-cua"
    assert evidence["transport_event"]["item"]["result"]["content"][0]["text"] == "Observed page"


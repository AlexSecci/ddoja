"""Persistent subscription-backed host for scoped, nonbrowser document tasks.

Protocol: https://learn.chatgpt.com/docs/app-server . Browser connector isolation
is deliberately not asserted. Qualified session handles prevent transport drift.
"""
from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import uuid

from .agent_runtime import (CodexAgentRuntime, FINAL_SCHEMA, AgentRuntimeError,
                            _final_response, _is_missing_session_error, _is_quota_error)
from .cli_paths import codex_invocation_env


class ProtocolError(AgentRuntimeError):
    def __init__(self, error):
        self.error = error
        super().__init__(str(error.get("message", "App-server request failed")))


class CodexAppServerRuntime:
    def __init__(self, root, codex_bin=None, *, store, process_factory=subprocess.Popen,
                 request_timeout=30, login_checker=None):
        self.root = Path(root).resolve()
        self.store = store
        self.cli = CodexAgentRuntime(root, codex_bin, login_checker=login_checker)
        self.process_factory = process_factory
        self.request_timeout = request_timeout
        self._process = None
        self._lock = threading.RLock()
        self._start_lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._pending = {}
        self._active = {}
        self._loaded = set()
        self._sequence = 0
        self._closed = False
        self._initialized = False
        self._tools = ThreadPoolExecutor(max_workers=8, thread_name_prefix="document-tools")
        self.registry_path = self.root / "data/agent-runtime/app-server-sessions.json"
        try:
            self._registry = json.loads(self.registry_path.read_text(encoding="utf-8"))
            if not isinstance(self._registry, dict):
                self._registry = {}
        except (OSError, ValueError):
            self._registry = {}

    @staticmethod
    def supports_session(session_id):
        return isinstance(session_id, str) and session_id.startswith("app_server:")

    def status(self):
        with self._lock:
            running = self._process is not None and self._process.poll() is None
            return {"mode": "app_server", "status": "closed" if self._closed else "ready" if running and self._initialized else "starting" if running else "idle",
                    "ready": running and self._initialized, "browser_tool_support": "unverified", "active_turns": len(self._active),
                    "detail": "Document tasks only; browser tasks use the proven CLI transport."}

    def _send(self, message):
        with self._write_lock:
            process = self._process
            if process is None or process.poll() is not None:
                raise AgentRuntimeError("The app-server connection is unavailable.")
            process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
            process.stdin.flush()

    def _request(self, method, params):
        with self._lock:
            self._sequence += 1
            identity = self._sequence
            future = self._pending[identity] = Future()
        try:
            self._send({"id": identity, "method": method, "params": params})
            return future.result(timeout=self.request_timeout)
        finally:
            with self._lock:
                self._pending.pop(identity, None)

    def _disconnected(self, process):
        with self._lock:
            if process is not self._process:
                return
            self._initialized = False
            self._loaded.clear()
            pending = list(self._pending.values())
            active = list(self._active.values())
        for future in pending:
            if not future.done():
                future.set_exception(AgentRuntimeError("The app-server connection closed."))
        for binding in active:
            binding["events"].put({"method": "host/disconnected", "params": {}})

    def _read(self, process):
        try:
            for line in process.stdout:
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if "id" in message and "method" not in message:
                    with self._lock:
                        future = self._pending.get(message["id"])
                    if future and not future.done():
                        if "error" in message:
                            future.set_exception(ProtocolError(message["error"]))
                        else:
                            future.set_result(message.get("result", {}))
                elif "id" in message:
                    self._tools.submit(self._handle_request, message)
                else:
                    params = message.get("params") or {}
                    thread_id = params.get("threadId")
                    with self._lock:
                        binding = self._active.get(thread_id)
                    if binding:
                        binding["events"].put(message)
        finally:
            self._disconnected(process)

    def _handle_request(self, message):
        params = message.get("params") or {}
        with self._lock:
            binding = self._active.get(params.get("threadId"))
        timing_key = None
        try:
            if message.get("method") != "item/tool/call" or not binding:
                raise AgentRuntimeError("Only a current scoped document tool request is supported.")
            if not binding["ready"].wait(5) or params.get("turnId") != binding.get("turn_id"):
                raise AgentRuntimeError("Tool call does not belong to the active document turn.")
            if binding["stop"]():
                raise AgentRuntimeError("The document task has stopped.")
            name = params.get("tool", "")
            if name.startswith("agent_bridge."):
                name = name.removeprefix("agent_bridge.")
            timing_key = str(message["id"])
            binding["timing"].tool_start(timing_key, "agent_bridge." + name, "other")
            value, images = binding["bridge"].invoke(name, params.get("arguments", {}))
            content = [{"type": "inputText", "text": json.dumps(value, ensure_ascii=False, separators=(",", ":"))}]
            content.extend({"type": "inputImage", "imageUrl": "data:" + image["mimeType"] + ";base64," + image["data"]}
                           for image in images)
            response = {"contentItems": content, "success": True}
            binding["events"].put({"method": "host/tool_result", "params": {"name": name}})
        except Exception as error:
            response = {"contentItems": [{"type": "inputText", "text": str(error)[:2000]}], "success": False}
        try:
            self._send({"id": message["id"], "result": response})
        except (OSError, AgentRuntimeError):
            pass
        finally:
            if timing_key is not None:
                binding["timing"].tool_end(timing_key, response.get("success", False))

    def _ensure(self):
        with self._start_lock:
            if self._closed:
                raise AgentRuntimeError("The app-server host is closed.")
            if self._process is not None and self._process.poll() is None:
                return
            if self._process is not None:
                self._disconnected(self._process)
            env = codex_invocation_env(self.root, "app-server-" + uuid.uuid4().hex, os.environ.copy())
            for key in ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN"):
                env.pop(key, None)
            if not self.cli._login_ok(env):
                raise AgentRuntimeError("Existing ChatGPT sign-in is required.")
            process = self.process_factory([self.cli.codex, "app-server"], cwd=self.root, env=env,
                                           stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                           text=True, encoding="utf-8", errors="replace")
            self._process = process
            threading.Thread(target=self._read, args=(process,), daemon=True).start()
            # Drain private diagnostics without forwarding credentials or arbitrary context.
            def drain():
                for _ in process.stderr:
                    pass
            threading.Thread(target=drain, daemon=True).start()
            try:
                self._request("initialize", {"clientInfo": {"name": "job_pipeline", "version": "2"},
                                             "capabilities": {"experimentalApi": True}})
                self._send({"method": "initialized", "params": {}})
                self._initialized = True
            except Exception:
                process.terminate()
                raise

    def _config(self):
        settings = self.cli._read_cli_settings()
        servers = {name: {"enabled": False} for name in settings.get("mcp_servers", {})}
        servers["cua_repl"] = {"enabled": False, "command": sys.executable, "args": []}
        return {"model_provider": "openai", "forced_login_method": "chatgpt", "web_search": "disabled",
                "mcp_servers": servers, "plugins": {name: {"enabled": False} for name in settings.get("plugins", {})},
                "features": {"shell_tool": False}}

    def run(self, launch, on_event, should_stop):
        from .timing import RunTiming
        from tools.agent_mcp import AgentMCPBridge, render_and_register_documents
        task = self.cli._normalize_launch_context(launch)
        worker = self.cli._worker(task)
        if worker["role"] != "document":
            return {"disposition": "failed", "error": "App-server browser connector isolation is unverified; use exec for browser roles."}
        capability = task.get("capability")
        session = worker.get("session_id")
        if not capability or session and not self.supports_session(session):
            return {"disposition": "failed", "error": "The saved session belongs to another transport."}
        choice = self.cli._choice(worker)
        bridge = AgentMCPBridge(worker["id"], capability, "document", self.store.agent_action,
                                lambda value: render_and_register_documents(self.store, worker["id"], capability, value),root=self.root)
        thread_id = session.removeprefix("app_server:") if session else None
        binding = {"events": queue.Queue(), "ready": threading.Event(), "bridge": bridge, "timing": RunTiming(),
                   "stop": should_stop, "turn_id": None}
        started = time.monotonic()
        turn_id = None
        try:
            self._ensure()
            if thread_id:
                if self._registry.get(thread_id) != {"worker_id": worker["id"], "role": "document"}:
                    return {"disposition": "session_missing", "session_id": session,
                            "error": "The qualified app-server session has no matching durable ownership record."}
                if thread_id not in self._loaded:
                    self._request("thread/resume", {"threadId": thread_id, "excludeTurns": True,
                                  "model": choice["model"], "cwd": str(self.root), "sandbox": "read-only",
                                  "approvalPolicy": "never", "config": self._config()})
            else:
                definitions = [{"type": "function", **{k: tool[k] for k in ("name", "description", "inputSchema")}}
                               for tool in bridge.tools()]
                result = self._request("thread/start", {"model": choice["model"], "cwd": str(self.root),
                    "sandbox": "read-only", "approvalPolicy": "never", "config": self._config(),
                    "dynamicTools": [{"type": "namespace", "name": "agent_bridge",
                                      "description": "Scoped document task tools", "tools": definitions}]})
                thread_id = result["thread"]["id"]
                session = "app_server:" + thread_id
                with self._lock:
                    self._registry[thread_id] = {"worker_id": worker["id"], "role": "document"}
                    self.registry_path.parent.mkdir(parents=True, exist_ok=True)
                    temporary = self.registry_path.with_suffix(".tmp")
                    temporary.write_text(json.dumps(self._registry), encoding="utf-8")
                    os.replace(temporary, self.registry_path)
            with self._lock:
                if thread_id in self._active:
                    raise AgentRuntimeError("This document session already has an active turn.")
                self._loaded.add(thread_id)
                self._active[thread_id] = binding
            on_event({"type": "thread.resumed" if worker.get("session_id") else "thread.started", "thread_id": session})
            binding["timing"].model_start()
            result = self._request("turn/start", {"threadId": thread_id, "model": choice["model"],
                "effort": choice.get("effort", "high"), "outputSchema": FINAL_SCHEMA,
                "input": [{"type": "text", "text": self.cli._prompt(task, worker)}]})
            turn_id = binding["turn_id"] = result["turn"]["id"]
            binding["ready"].set()
            final, usage, calls, interrupted_at = None, None, 0, None
            while True:
                if should_stop() and interrupted_at is None:
                    interrupted_at = time.monotonic()
                    self._request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})
                if interrupted_at is not None and time.monotonic() - interrupted_at > 10:
                    self._process.terminate()
                    return {"disposition": "interrupted", "session_id": session, "error": "Interrupt acknowledgment timed out; saved session retained."}
                try:
                    event = binding["events"].get(timeout=.2)
                except queue.Empty:
                    continue
                method, params = event["method"], event.get("params") or {}
                if method == "host/disconnected":
                    raise AgentRuntimeError("The persistent host disconnected during a turn.")
                if method == "host/tool_result":
                    calls += 1
                    on_event({"type": "tool_result", "tool_name": "agent_bridge." + params["name"],
                              "success": True, "session_id": session})
                    continue
                on_event({"type": method, "session_id": session, "transport_event": event})
                item = params.get("item") or {}
                if method == "item/started" and item.get("type") in {"commandExecution", "fileChange", "webSearch", "mcpToolCall", "collabToolCall"}:
                    self._request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})
                    raise AgentRuntimeError("A document task attempted a tool outside its scoped bridge.")
                if method == "item/completed" and item.get("type") == "agentMessage":
                    final = _final_response({"type": "item.completed", "item": {"type": "agent_message", "text": item.get("text", "")}})
                if method == "thread/tokenUsage/updated":
                    # `last` covers one model request, not the whole turn.
                    # A turn can include many requests separated by tools.
                    usage = (params.get("tokenUsage") or {}).get("total")
                if method == "turn/completed":
                    turn = params.get("turn") or {}
                    error = turn.get("error") or {}
                    if usage:
                        on_event({"type": "usage", "session_id": session, "turn_id": turn_id, "counter_kind": "cumulative",
                            "counters": {"input_tokens": usage.get("inputTokens"), "output_tokens": usage.get("outputTokens"),
                                         "cached_input_tokens": usage.get("cachedInputTokens"), "reasoning_tokens": usage.get("reasoningOutputTokens")},
                            "tool_call_count": calls, "elapsed_seconds": time.monotonic() - started, "counter_source": "app_server.tokenUsage.total"})
                    if _is_quota_error(error):
                        return {"disposition": "quota_paused", "session_id": session, "provider_error": error}
                    if turn.get("status") == "interrupted":
                        return {"disposition": "interrupted", "session_id": session}
                    if turn.get("status") != "completed" or final is None:
                        return {"disposition": "failed", "session_id": session, "error": str(error or "Missing structured document result")}
                    return {**final, "session_id": session, "elapsed_seconds": time.monotonic() - started}
        except ProtocolError as error:
            return {"disposition": "session_missing" if session and _is_missing_session_error(error.error) else
                    "quota_paused" if _is_quota_error(error.error) else "failed", "session_id": session,
                    "error": str(error)[:2000], "provider_error": error.error}
        except Exception as error:
            return {"disposition": "failed", "session_id": session, "error": str(error)[:2000]}
        finally:
            summary = binding["timing"].summary()
            try:
                on_event({"type": "timing_summary", "session_id": session, "timing": summary})
            except Exception:
                # Preserve diagnostics even if stop revokes the database callback.
                path = self.root / "data/agent-runtime" / ("timing-" + summary["run_id"] + ".json")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(summary), encoding="utf-8")
            if thread_id:
                with self._lock:
                    if self._active.get(thread_id) is binding:
                        self._active.pop(thread_id, None)

    def close(self):
        with self._start_lock:
            if self._closed:
                return
            self._closed = True
            process = self._process
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
            self._tools.shutdown(wait=True, cancel_futures=True)

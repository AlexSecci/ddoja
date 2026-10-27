"""Subscription-only bounded CLI transport for non-browser workers."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import time
import tomllib
import uuid

from .cli_paths import CODEX_CLI_NOT_FOUND, codex_invocation_env, resolve_codex_cli


class CodexRunner:
    def __init__(self, root, codex_bin=None):
        self.root = Path(root).resolve()
        self.codex = resolve_codex_cli(codex_bin)
        if not self.codex:
            raise RuntimeError(CODEX_CLI_NOT_FOUND)
        self.config = json.loads((self.root / "config" / "workers.json").read_text(encoding="utf-8"))
        if self.config.get("authentication") != "chatgpt":
            raise RuntimeError("This runner requires the user's existing ChatGPT subscription.")

    def run(self, stage, prompt, schema, images=()):
        if stage not in {"evaluation", "materials", "review"}:
            raise ValueError("This transport supports non-browser evaluation, materials and review workers only.")
        run_id = uuid.uuid4().hex
        folder = self.root / "data" / "runs" / run_id
        folder.mkdir(parents=True)
        schema_path = folder / "schema.json"
        schema_path.write_text(json.dumps(schema), encoding="utf-8")
        choice = self.config["models"]["materials" if stage == "review" else stage]
        args = [self.codex, "exec", "--json", "--ephemeral", "--skip-git-repo-check",
                "--sandbox", "read-only", "--color", "never", "--model", choice["model"],
                "-c", 'model_provider="openai"', "-c", 'forced_login_method="chatgpt"',
                "-c", f'model_reasoning_effort="{choice["effort"]}"',
                "-c", 'web_search="disabled"', "--output-schema", str(schema_path), "-"]
        for path in images:
            args[-1:-1] = ["--image", str(path)]
        # Use documented per-run plugin/server settings; never mutate global configuration.
        config_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
        config_path = config_home / "config.toml"
        configured = tomllib.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
        for plugin in configured.get("plugins", {}):
            if "." in plugin:
                raise RuntimeError("Per-run plugin keys containing dots require a supported configuration adapter.")
            args.extend(["-c", f"plugins.{plugin}.enabled=false"])
        for server in configured.get("mcp_servers", {}):
            if "." in server:
                raise RuntimeError("Per-run MCP keys containing dots require a supported configuration adapter.")
            args.extend(["-c", f"mcp_servers.{server}.enabled=false"])
        env = codex_invocation_env(self.root, run_id, os.environ.copy())
        for key in ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN"):
            env.pop(key, None)
        check = subprocess.run([self.codex, "login", "status"], env=env, capture_output=True,
                               text=True, encoding="utf-8", errors="replace", timeout=20)
        if check.returncode or "ChatGPT" not in check.stdout + check.stderr:
            raise RuntimeError("ChatGPT sign-in is required. API-key fallback is disabled.")
        start = time.monotonic()
        process = subprocess.Popen(args, cwd=self.root, env=env, stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, encoding="utf-8", errors="replace")
        try:
            stdout, stderr = process.communicate(prompt, timeout=self.config["worker_timeout_seconds"])
        except subprocess.TimeoutExpired:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, timeout=15)
            else:
                process.kill()
            process.communicate(timeout=15)
            raise RuntimeError("Worker timed out; no result was committed.")
        events = []
        for line in stdout.splitlines():
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                pass
        result = None
        for event in events:
            item = event.get("item", {})
            if event.get("type") == "item.completed" and item.get("type") == "agent_message":
                try:
                    result = json.loads(item.get("text", ""))
                except json.JSONDecodeError:
                    pass
        completed = any(e.get("type") == "turn.completed" for e in events)
        report = {"run_id": run_id, "stage": stage, "requested_model": choice["model"],
                  "effort": choice["effort"], "authentication": "chatgpt", "returncode": process.returncode,
                  "elapsed_seconds": round(time.monotonic() - start, 2), "completed": completed,
                  "usage": [e.get("usage") for e in events if e.get("type") == "turn.completed"],
                  "error_events": [e for e in events if e.get("type") in {"error", "turn.failed"}],
                  "stderr_tail": stderr[-3000:]}
        (folder / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        if process.returncode or not completed or result is None:
            raise RuntimeError(f"Worker did not complete a structured response. Report: {run_id}")
        # Reject unexpected external tool activity even when a response was produced.
        if any(e.get("item", {}).get("type") in {"mcp_tool_call", "web_search"} for e in events):
            raise RuntimeError("Non-browser worker used an unexpected external tool; result rejected.")
        (folder / "response.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        return result, report

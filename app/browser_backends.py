"""Provider-independent Playwright MCP launch configuration; native CUA is retained."""
import json
import os
from pathlib import Path
import re
import shutil

PLAYWRIGHT_SERVER = "playwright"
PLAYWRIGHT_TOOLS = ["browser_navigate", "browser_navigate_back", "browser_snapshot", "browser_fill_form", "browser_type",
                    "browser_click", "browser_select_option", "browser_press_key", "browser_wait_for", "browser_file_upload",
                    "browser_take_screenshot", "browser_close", "browser_tabs", "browser_handle_dialog"]


def playwright_status(root):
    root = Path(root)
    node = shutil.which("node")
    try:
        node = json.loads((root / "config/runtime.json").read_text()).get("node") or node
    except (OSError, ValueError):
        pass
    cli = root / "browser-runtime/node_modules/@playwright/mcp/cli.js"
    browser = next((p for base in (os.environ.get("PROGRAMFILES", ""), os.environ.get("PROGRAMFILES(X86)", ""), os.environ.get("LOCALAPPDATA", ""))
                    for p in (Path(base) / "Google/Chrome/Application/chrome.exe", Path(base) / "Microsoft/Edge/Application/msedge.exe") if p.is_file()), None)
    return {"ready": bool(node and Path(node).is_file() and cli.is_file() and browser), "node": node,
            "cli": str(cli), "browser": str(browser) if browser else None,
            "setup_command": "npm ci --prefix browser-runtime --ignore-scripts --no-audit --no-fund"}


def playwright_args(root, worker):
    status = playwright_status(root)
    if not status["ready"]:
        raise RuntimeError("Playwright browser runtime is not installed. Open Settings for the setup command.")
    worker_id = worker["id"]
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", worker_id):
        raise ValueError("Invalid browser profile identity.")
    folder = Path(root) / "data/browser-profiles" / worker_id
    folder.mkdir(parents=True, exist_ok=True)
    config = {"browser": {"browserName": "chromium", "userDataDir": str(folder / "profile"),
                          "launchOptions": {"executablePath": status["browser"], "headless": True, "chromiumSandbox": True}},
              "outputDir": str(folder / "output")}
    path = folder / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    prefix = "mcp_servers." + PLAYWRIGHT_SERVER
    args = ["-c", prefix + ".command=" + json.dumps(status["node"]),
            "-c", prefix + ".args=" + json.dumps([status["cli"], "--config", str(path)]),
            "-c", prefix + ".enabled=true", "-c", prefix + ".required=true",
            "-c", prefix + ".startup_timeout_sec=30", "-c", prefix + ".tool_timeout_sec=90",
            "-c", prefix + ".enabled_tools=" + json.dumps(PLAYWRIGHT_TOOLS)]
    # Only this worker's dedicated browser is in scope. Host-side application
    # checkpoints, actual human consent and final-send checks remain required.
    for tool in PLAYWRIGHT_TOOLS:
        args += ["-c", prefix + ".tools." + tool + '.approval_mode="approve"']
    return args

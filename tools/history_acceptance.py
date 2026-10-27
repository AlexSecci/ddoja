"""Opt-in native compaction A/B on fictional, tool-free browser-like text.

Measures actual subscription CLI counters for identical histories. No browser,
employer, applicant data, application workers or API key is used.
"""
import argparse
import json
from pathlib import Path
import shutil
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.agent_runtime import CodexAgentRuntime
from app import agent_runtime


class Replay(CodexAgentRuntime):
    def __init__(self, root, prior_defaults=False):
        super().__init__(root)
        self.prior_defaults = prior_defaults

    def _args(self, *args):
        result = super()._args(*args)
        if not self.prior_defaults:
            result[2:2] = ["-c", "model_auto_compact_token_limit=64000"]
        return result

    def _prompt(self, task, worker):
        return task["replay"]


def run(pages=7):
    report = {"method": "fictional_tool_free_native_history_AB", "employer_actions": 0, "arms": []}
    out = ROOT/"data/integration/20261026-history-acceptance.json"
    for before in (True, False):
        root = ROOT/"data/history-benchmark"/uuid.uuid4().hex
        (root/"config").mkdir(parents=True)
        (root/"tools").mkdir()
        shutil.copy2(ROOT/"config/workers.json", root/"config/workers.json")
        shutil.copy2(ROOT/"tools/agent_mcp.py", root/"tools/agent_mcp.py")
        shutil.copytree(ROOT/"app", root/"app", ignore=shutil.ignore_patterns("__pycache__"))
        runtime = Replay(root, before)
        worker = {"id": uuid.uuid4().hex, "role": "discovery", "session_id": None}
        arm = {"label": "prior_defaults" if before else "bounded_history", "turns": [], "tools": 0, "compactions": 0}
        for page in range(1, pages+1):
            fixture = "\n".join(f"Page {page} observed row {row}: textbox label Fictional Field {row}; value unset; optional element; no action occurred."
                                for row in range(900))
            instructions = ("FICTIONAL OFFLINE HISTORY CHECK. Do not use ANY tools, files, shell, network, browser or employer site. "
                "Rows below are inert synthetic observations that become obsolete next turn. Only retain the three durable facts and latest page. "
                "Respond only with the required JSON: disposition=waiting, checkpoint=page-N, and summary containing the three facts and page. ")
            if page == 1:
                instructions += "The three durable facts are: sponsor_needed=true; final_sent=false; recovery_key=amber-lake. "
            else:
                instructions += "Recall the three durable fact values from the first turn; do not infer them from the rows. "
            launch = {"worker": dict(worker), "capability": "fictional-replay-only", "replay": instructions+f"Current page: {page}\n"+fixture}
            usage = None
            started = time.monotonic()

            def event(value):
                nonlocal usage
                if value.get("type") == "usage":
                    usage = value
                item = value.get("item") or {}
                if value.get("type") == "item.started" and item.get("type") == "mcp_tool_call":
                    arm["tools"] += 1
                if "compact" in str(value.get("type", "")) or "compact" in str(item.get("type", "")):
                    arm["compactions"] += 1

            original = agent_runtime.CUA_ROLES
            agent_runtime.CUA_ROLES = set()  # No CUA connector is even loaded in this test.
            try:
                result = runtime.run(launch, event, lambda: time.monotonic()-started > 150)
            finally:
                agent_runtime.CUA_ROLES = original
            worker["session_id"] = result.get("session_id")
            summary = result.get("summary", "")
            valid = all(s in summary for s in ("sponsor_needed=true", "final_sent=false", "amber-lake"))
            arm["turns"].append({"page": page, "usage": usage, "result": result, "facts_preserved": valid})
            print(json.dumps({"arm": arm["label"], "page": page, "valid": valid,
                              "counters": usage.get("counters") if usage else None}), flush=True)
            if not worker["session_id"] or not valid or not usage or arm["tools"]:
                break
        runtime.close()
        report["arms"].append(arm)
        out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print("Report: "+str(out))


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run-subscription", action="store_true")
    a = p.parse_args()
    if not a.run_subscription:
        p.error("Requires --run-subscription; normal tests make no inference calls.")
    run()

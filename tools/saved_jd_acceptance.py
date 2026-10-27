"""Opt-in real evaluator check against saved JDs in isolated workspaces.

Models retain judgment. Expected dispositions are an offline regression rubric;
the rubric is never imported by production code. No browser or application runs.
"""
import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import agent_runtime
from app.store import Store


def run(database):
    reports = []
    with closing(sqlite3.connect(Path(database).resolve().as_uri() + "?mode=ro", uri=True)) as original:
        original.row_factory = sqlite3.Row
        for job_id, expected in [("c614ae4cc4e144e4a489e71cdbdb6b6f", "approve"),
                                 ("895ef7d04db44f0a93665224795743cb", "discard")]:
            root = ROOT / "data/efficiency-evaluation" / uuid.uuid4().hex
            root.mkdir(parents=True)
            shutil.copytree(ROOT / "app", root / "app", ignore=shutil.ignore_patterns("__pycache__"))
            shutil.copytree(ROOT / "prompts", root / "prompts")
            (root / "tools").mkdir()
            shutil.copy2(ROOT / "tools/agent_mcp.py", root / "tools/agent_mcp.py")
            shutil.copytree(ROOT / "config", root / "config")
            path = root / "prompts/evaluation_agent.md"
            path.write_text("OFFLINE SAVED-JD REGRESSION. No browser, network, forms, uploads, employer action or application submission. "
                            "Use the complete saved JD and exact supplied applicant sources. If a missing applicant fact matters, "
                            "ask through this isolated inbox; no human will be contacted by this test.\n\n" + path.read_text(encoding="utf-8"), encoding="utf-8")
            store = Store(root)
            with store.connect(write=True) as db:
                for table in ("source_documents", "document_versions", "technologies", "technology_aliases"):
                    columns = [row[1] for row in original.execute("PRAGMA table_info(" + table + ")")]
                    for row in original.execute("SELECT * FROM " + table):
                        db.execute("INSERT OR REPLACE INTO " + table + " (" + ",".join(columns) + ") VALUES(" +
                                   ",".join("?" for _ in columns) + ")", tuple(row))
                db.execute("UPDATE meta SET value=? WHERE key='profile_version'",
                           (original.execute("SELECT value FROM meta WHERE key='profile_version'").fetchone()[0],))
            job = original.execute("SELECT j.*,v.jd,v.metadata FROM jobs j JOIN job_versions v "
                                   "ON v.job_id=j.id AND v.version=j.jd_version WHERE j.id=?", (job_id,)).fetchone()
            pipeline = store.create_agent_pipeline("OFFLINE SAVED JD", "https://offline.invalid/careers")
            discovery = store.list_agent_workers(pipeline["id"])[0]
            store.reserve_agent_worker(discovery["id"])
            d = store.agent_launch_context(discovery["id"])
            listing = store.agent_action(discovery["id"], d["capability"], "record_listing", {
                "url": "https://offline.invalid/" + job_id, "title": job["title"], "jd": job["jd"],
                "requisition_id": job["requisition_id"], "metadata": json.loads(job["metadata"]), "selected": True})
            evaluator = store.get_agent_worker(listing["evaluator_worker_id"])
            store.reserve_agent_worker(evaluator["id"])
            launch = store.agent_launch_context(evaluator["id"])
            runtime = agent_runtime.CodexAgentRuntime(root)
            started = time.monotonic()
            actions = []

            def on_event(event):
                store.agent_runtime_event(evaluator["id"], event)
                if event.get("type") == "usage":
                    store.record_agent_usage(evaluator["id"], event)
                item = event.get("item") or {}
                if item.get("type") == "mcp_tool_call":
                    actions.append((item.get("server"), item.get("tool")))

            # Test-only capability restriction; the shipped evaluator keeps CUA
            # available for JD repair. This process never starts a service.
            saved_roles = agent_runtime.CUA_ROLES
            agent_runtime.CUA_ROLES = set()
            try:
                result = runtime.run(launch, on_event, lambda: time.monotonic() - started > 150)
            finally:
                agent_runtime.CUA_ROLES = saved_roles
            final = store.finish_agent_turn(evaluator["id"], result)
            worker = store.get_agent_worker(evaluator["id"])
            with store.connect() as db:
                committed = db.execute("SELECT agent_decision,agent_reason FROM pipeline_jobs WHERE pipeline_id=? AND job_id=?",
                                       (pipeline["id"], listing["job_id"])).fetchone()
            reports.append({"job_id": job_id, "title": job["title"], "workspace": str(root),
                            "jd_sha256": hashlib.sha256(job["jd"].encode()).hexdigest(),
                            "expected": expected, "decision": committed["agent_decision"],
                            "reason": committed["agent_reason"], "transport": result,
                            "only_scoped_bridge": all(server == "agent_bridge" for server, _ in actions),
                            "usage": store.get_agent_efficiency(pipeline["id"]),
                            "questions": store.agent_inbox(pipeline["id"]), "employer_actions": 0})
    target = ROOT / "data/integration/20261023-saved-jd-acceptance.json"
    target.write_text(json.dumps({"test_only": True, "cases": reports,
        "limits": "Two semantic regression cases, not a statistical quality or end-to-end speed estimate."}, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(target), "cases": [{key: case[key] for key in ("title", "expected", "decision", "only_scoped_bridge")} for case in reports]}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True)
    parser.add_argument("--run-subscription", action="store_true")
    args = parser.parse_args()
    if not args.run_subscription:
        parser.error("Explicit --run-subscription is required.")
    run(args.database)

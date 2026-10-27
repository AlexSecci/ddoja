"""Opt-in, isolated subscription A/B benchmark. Never contacts an employer.

Each case is one independent evaluator with identical saved evidence. Records
actual model counters, durable judgments and exact input hashes, not estimates.
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

CASES = [
    ("c614ae4cc4e144e4a489e71cdbdb6b6f", "approve"),
    ("895ef7d04db44f0a93665224795743cb", "discard"),
    ("0af5429c40ae49fcb2746325986f1703", "approve"),
]


def run(label, database, limit=3):
    report = {"label": label, "employer_actions": 0, "cases": []}
    target = ROOT / "data/integration" / ("20261025-tokens-" + label + ".json")
    with closing(sqlite3.connect(Path(database).resolve().as_uri()+"?mode=ro", uri=True)) as original:
        original.row_factory = sqlite3.Row
        for job_id, expected in CASES[:limit]:
            root = ROOT / "data/token-benchmark" / label / uuid.uuid4().hex
            root.mkdir(parents=True)
            for name in ("app", "prompts", "config"):
                shutil.copytree(ROOT/name, root/name, ignore=shutil.ignore_patterns("__pycache__"))
            (root/"tools").mkdir()
            shutil.copy2(ROOT/"tools/agent_mcp.py", root/"tools/agent_mcp.py")
            role = root/"prompts/evaluation_agent.md"
            role.write_text("OFFLINE ACCEPTANCE: No browser, network, employer actions, uploads or forms. "
                            "Assess the complete saved description and confirmed evidence through the scoped bridge.\n\n"
                            + role.read_text(encoding="utf-8"), encoding="utf-8")
            store = Store(root)
            with store.connect(write=True) as db:
                for table in ("source_documents", "document_versions", "technologies", "technology_aliases"):
                    columns = [r[1] for r in original.execute("PRAGMA table_info("+table+")")]
                    for row in original.execute("SELECT * FROM "+table):
                        db.execute("INSERT OR REPLACE INTO "+table+" ("+",".join(columns)+") VALUES("
                                   +",".join("?" for _ in columns)+")", tuple(row))
                db.execute("UPDATE meta SET value=? WHERE key='profile_version'", (
                    original.execute("SELECT value FROM meta WHERE key='profile_version'").fetchone()[0],))
            job = original.execute("SELECT j.*,v.jd,v.metadata FROM jobs j JOIN job_versions v ON v.job_id=j.id "
                                   "AND v.version=j.jd_version WHERE j.id=?", (job_id,)).fetchone()
            pipeline = store.create_agent_pipeline("ISOLATED TOKEN BENCHMARK", "https://offline.invalid/careers")
            discovery = store.list_agent_workers(pipeline["id"])[0]
            store.reserve_agent_worker(discovery["id"])
            launch = store.agent_launch_context(discovery["id"])
            listing = store.agent_action(discovery["id"], launch["capability"], "record_listing", {
                "url": "https://offline.invalid/"+job_id, "title": job["title"], "jd": job["jd"],
                "requisition_id": job["requisition_id"], "metadata": json.loads(job["metadata"]), "selected": True})
            worker_id = listing["evaluator_worker_id"]
            store.reserve_agent_worker(worker_id)
            launch = store.agent_launch_context(worker_id)
            runtime = agent_runtime.CodexAgentRuntime(root)
            started = time.monotonic()
            calls = []

            def event(value):
                store.agent_runtime_event(worker_id, value)
                if value.get("type") == "usage":
                    store.record_agent_usage(worker_id, value)
                item = value.get("item") or {}
                if value.get("type") == "item.completed" and item.get("type") == "mcp_tool_call":
                    calls.append((item.get("server"), item.get("tool")))

            old = agent_runtime.CUA_ROLES
            agent_runtime.CUA_ROLES = set()  # Enforced test-only offline restriction in BOTH arms.
            try:
                transport = runtime.run(launch, event, lambda: time.monotonic()-started > 180)
            finally:
                agent_runtime.CUA_ROLES = old
                runtime.close()
            store.finish_agent_turn(worker_id, transport)
            with store.connect() as db:
                decision = dict(db.execute("SELECT agent_decision,agent_reason FROM pipeline_jobs WHERE pipeline_id=? AND job_id=?",
                                          (pipeline["id"], listing["job_id"])).fetchone())
            worker = store.get_agent_worker(worker_id)
            case = {"job_id": job_id, "title": job["title"], "expected": expected, **decision,
                    "jd_sha256": hashlib.sha256(job["jd"].encode()).hexdigest(), "workspace": str(root),
                    "usage": store.get_agent_efficiency(pipeline["id"])["totals"], "tools": calls,
                    "transport": transport, "findings": worker["result"],
                    "launch_characters": len(json.dumps(launch["task"]))}
            report["cases"].append(case)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
            print(json.dumps({"label": label, "job": job_id, "expected": expected, **decision,
                              "usage": case["usage"]}), flush=True)
    print("Report: "+str(target))


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--label", required=True)
    p.add_argument("--database", required=True)
    p.add_argument("--limit", type=int, default=3, choices=[1, 2, 3])
    p.add_argument("--run-subscription", action="store_true")
    a = p.parse_args()
    if not a.run_subscription:
        p.error("Requires --run-subscription; normal tests make no model calls.")
    run(a.label, a.database, a.limit)

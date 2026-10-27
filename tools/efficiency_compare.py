"""Compare stored legacy contexts with new contexts in a disposable snapshot.

No model is called. No employer is contacted. This measures bytes/latency and
verifies exact JD/source transport, not fit-decision quality or token savings.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.store import Store
from tools.efficiency_benchmark import completed_tool, text_blocks


def _characters(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def compare(database, artifacts=None):
    database = Path(database).resolve(strict=True)
    old = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    old.row_factory = sqlite3.Row
    latest = {}
    for row in old.execute("SELECT e.*,w.role FROM agent_runtime_events e JOIN agent_workers w "
                           "ON w.id=e.worker_id ORDER BY e.created_at,e.rowid"):
        payload = json.loads(row["payload"])
        completed = completed_tool(payload)
        if completed:
            item, result = completed
            name = row["tool_name"] or ".".join(filter(None, (item.get("server"), item.get("tool"))))
            if name == "agent_bridge.get_context":
                for text in text_blocks(result):
                    try:
                        latest[row["role"]] = (row["worker_id"], json.loads(text))
                    except ValueError:
                        pass
    with tempfile.TemporaryDirectory(prefix="job-pipeline-efficiency-") as temp:
        root = Path(temp)
        (root / "data").mkdir()
        with closing(sqlite3.connect(root / "data/pipeline.sqlite")) as copy:
            old.backup(copy)
        old.close()
        if artifacts:
            shutil.copytree(Path(artifacts), root / "artifacts")
        store = Store(root)
        reports = {}
        with store.connect() as db:
            for role, (worker_id, prior) in latest.items():
                worker = db.execute("SELECT w.*,p.company_id,p.stop_requested,p.status AS pipeline_status "
                                    "FROM agent_workers w JOIN pipelines p ON p.id=w.pipeline_id WHERE w.id=?",
                                    (worker_id,)).fetchone()
                start = time.perf_counter()
                current = store._agent_context_for_worker(
                    db, worker, {"sections": store._agent_initial_sections(role)})
                controls = store._agent_context_for_worker(db, worker, {})
                elapsed = time.perf_counter() - start
                bundle = current["sections"]
                jd = bundle.get("job", {}).get("jd") if isinstance(bundle.get("job"), dict) else None
                saved = db.execute("SELECT v.jd,v.sha256 FROM jobs j JOIN job_versions v "
                                   "ON v.job_id=j.id AND v.version=j.jd_version WHERE j.id=?",
                                   (worker["job_id"],)).fetchone() if worker["job_id"] else None
                if saved and jd != saved["jd"]:
                    raise AssertionError("Full JD changed during context transport.")
                reports[role] = {"worker_id": worker_id, "legacy_context_characters": _characters(prior),
                                 "new_initial_sections_characters": _characters(current),
                                 "new_controls_characters": _characters(controls),
                                 "build_pair_seconds": elapsed,
                                 "full_jd_exact": jd == saved["jd"] if saved else None,
                                 "jd_sha256": hashlib.sha256(jd.encode()).hexdigest() if jd else None}
            source_checks = []
            evidence_worker = db.execute("SELECT * FROM agent_workers WHERE role='evaluator' LIMIT 1").fetchone()
            if evidence_worker:
                for source in db.execute("SELECT id,content,sha256 FROM source_documents ORDER BY id"):
                    reply = store._agent_retrieve_sources(db, evidence_worker, {"source_ids": [source["id"]]})
                    returned = reply["sources"][0]
                    if returned["content"] != source["content"]:
                        raise AssertionError("Applicant source retrieval lost content.")
                    source_checks.append({"id": source["id"], "characters": len(source["content"]),
                                          "exact": True, "sha256": source["sha256"]})
        return {"method": "isolated_saved_snapshot_no_inference", "input_database": str(database),
                "roles": reports, "source_retrieval": source_checks,
                "limitations": ["Character counts are not tokens or subscription usage.",
                                "Legacy payloads are historical snapshots; current controls/facts may be newer.",
                                "This is not a fit-quality or end-to-end browser benchmark."]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--artifacts", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = compare(args.database, args.artifacts)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(args.output.resolve()), "roles": result["roles"]}))

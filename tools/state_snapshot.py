"""Consistent local pipeline baseline; no inference, browsing or job mutations."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.store import Store


def snapshot(label, pipeline_id):
    now = datetime.now(timezone.utc)
    folder = ROOT/"data/experiments"/(now.strftime("%Y%m%dT%H%M%SZ")+"-"+label)
    folder.mkdir(parents=True)
    target = folder/"pipeline.sqlite"
    with closing(sqlite3.connect((ROOT/"data/pipeline.sqlite").as_uri()+"?mode=ro", uri=True)) as source, \
            closing(sqlite3.connect(target)) as dest:
        source.backup(dest)
    store = Store.__new__(Store)
    store.root = ROOT
    store.path = target
    with store.connect() as db:
        pipeline = dict(db.execute("SELECT p.*,c.name AS company_name FROM pipelines p JOIN companies c ON c.id=p.company_id WHERE p.id=?", (pipeline_id,)).fetchone())
        queries = {
            "worker_counts": ("SELECT role,state,count(*) AS count FROM agent_workers WHERE pipeline_id=? GROUP BY role,state", (pipeline_id,)),
            "workers": ("SELECT id,job_id,parent_id,role,state,session_id,resume_mode,checkpoint,error,jd_version,profile_version,launch_generation,turn_active,created_at,updated_at FROM agent_workers WHERE pipeline_id=?", (pipeline_id,)),
            "jobs": ("SELECT pj.*,j.title,j.url,j.requisition_id,j.jd_version FROM pipeline_jobs pj JOIN jobs j ON j.id=pj.job_id WHERE pj.pipeline_id=?", (pipeline_id,)),
            "submission_results": ("SELECT s.* FROM agent_submission_results s JOIN agent_workers w ON w.id=s.worker_id WHERE w.pipeline_id=?", (pipeline_id,)),
            "inbox_counts": ("SELECT kind,state,count(*) AS count FROM agent_inbox WHERE pipeline_id=? GROUP BY kind,state", (pipeline_id,)),
            "browser_controls": ("SELECT * FROM agent_browser_controls", ()),
            "source_hashes": ("SELECT id,sha256,imported_at FROM source_documents ORDER BY id", ()),
        }
        state = {key: [dict(r) for r in db.execute(q, params)] for key, (q, params) in queries.items()}
        watermarks = {table: db.execute("SELECT COALESCE(MAX(rowid),0) FROM "+table).fetchone()[0]
                      for table in ("events", "agent_usage", "agent_runtime_events", "agent_submission_results", "agent_artifacts")}
        active = db.execute("SELECT COUNT(*) FROM agent_workers WHERE turn_active=1 OR state='running'").fetchone()[0]
        state.update({"snapshot_at_utc": now.isoformat(), "pipeline": pipeline, "watermarks": watermarks,
                      "profile_version": store.profile_version(db), "active_workers": active})
    state["efficiency"] = store.get_agent_efficiency(pipeline_id)
    state["database_sha256"] = hashlib.sha256(target.read_bytes()).hexdigest()
    state["database_bytes"] = target.stat().st_size
    state["code_sha256"] = {}
    with zipfile.ZipFile(folder/"source.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        for base in ("app", "prompts", "config", "tools", "tests"):
            for path in sorted((ROOT/base).rglob("*")):
                if path.is_file() and not set(path.parts)&{"__pycache__", ".pytest_cache"}:
                    relative = path.relative_to(ROOT).as_posix()
                    archive.write(path, relative)
                    state["code_sha256"][relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    (folder/"state.json").write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    pointer = ROOT/"data/experiments/current.json"
    pointer.write_text(json.dumps({"baseline": str(folder), "pipeline_id": pipeline_id,
                                   "started_at_utc": now.isoformat()}, indent=2), encoding="utf-8")
    print(json.dumps({"snapshot": str(folder), "pipeline": pipeline["company_name"], "state": pipeline["status"],
                      "jobs": len(state["jobs"]), "active_workers": active,
                      "usage_watermark": watermarks["agent_usage"], "database_sha256": state["database_sha256"]}))


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--label", default="before-quota-run")
    p.add_argument("--pipeline", required=True)
    a = p.parse_args()
    if not a.label.replace("-", "").replace("_", "").isalnum():
        p.error("Use letters, digits, hyphens or underscores for the label.")
    snapshot(a.label, a.pipeline)

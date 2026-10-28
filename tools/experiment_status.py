"""Compact read-only progress/timing delta for the current saved experiment."""
from datetime import datetime, timezone
import argparse
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.timing import summarize_timings


def status():
    pointer = json.loads((ROOT / "data/experiments/current.json").read_text(encoding="utf-8"))
    folder = Path(pointer["baseline"])
    baseline = json.loads((folder / "state.json").read_text(encoding="utf-8"))
    pid = pointer["pipeline_id"]
    with sqlite3.connect((ROOT / "data/pipeline.sqlite").as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        pipeline = dict(db.execute("SELECT id,status,stage,stop_requested FROM pipelines WHERE id=?", (pid,)).fetchone())
        counts = dict(db.execute("SELECT state,COUNT(*) FROM agent_workers WHERE pipeline_id=? GROUP BY state", (pid,)).fetchall())
        jobs = [dict(row) for row in db.execute("SELECT pj.*,j.title,j.requisition_id FROM pipeline_jobs pj JOIN jobs j ON j.id=pj.job_id WHERE pj.pipeline_id=?", (pid,))]
        def job_counts(rows):
            return {"discovered": len(rows), "selected": sum(bool(r["selected"]) for r in rows),
                    "evaluated": sum(r.get("agent_decision") in {"approve", "discard"} for r in rows),
                    "approved": sum(r.get("agent_decision") == "approve" for r in rows),
                    "submitted": sum(r.get("agent_status") == "submitted" for r in rows)}
        old_counts = job_counts(baseline["jobs"])
        new_counts = job_counts(jobs)
        watermark = baseline["watermarks"]
        rows = db.execute("SELECT e.payload FROM agent_runtime_events e JOIN agent_workers w ON w.id=e.worker_id "
                          "WHERE e.rowid>? AND w.pipeline_id=? AND e.event_type='timing_summary'",
                          (watermark["agent_runtime_events"], pid)).fetchall()
        timing = summarize_timings([json.loads(r[0]).get("timing", {}) for r in rows])
        pending = dict(db.execute("SELECT kind,COUNT(*) FROM agent_inbox WHERE pipeline_id=? AND state='pending' GROUP BY kind", (pid,)).fetchall())
        receipts = [dict(row) for row in db.execute(
            "SELECT r.id,r.job_id,r.state,r.created_at,j.requisition_id FROM agent_submission_results r "
            "JOIN jobs j ON j.id=r.job_id WHERE r.rowid>? AND r.pipeline_id=?", (watermark["agent_submission_results"], pid))]
        quota_row = db.execute("SELECT value FROM meta WHERE key='agent_quota_latch'").fetchone()
        usage = db.execute("SELECT COUNT(*),COALESCE(SUM(COALESCE(u.delta_input_tokens,u.input_tokens)),0),"
                           "COALESCE(SUM(COALESCE(u.delta_cached_input_tokens,u.cached_input_tokens)),0),"
                           "COALESCE(SUM(COALESCE(u.delta_output_tokens,u.output_tokens)),0) FROM agent_usage u JOIN agent_workers w ON w.id=u.worker_id "
                           "WHERE u.rowid>? AND w.pipeline_id=?", (watermark["agent_usage"], pid)).fetchone()
        provider_usage=None
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='agent_provider_requests'").fetchone():
            counts=db.execute('SELECT COUNT(*),COUNT(r.input_tokens),COALESCE(SUM(r.input_tokens),0),COALESCE(SUM(r.cached_input_tokens),0),COALESCE(SUM(r.output_tokens),0) FROM agent_provider_requests r JOIN agent_workers w ON w.id=r.worker_id WHERE r.rowid>? AND w.pipeline_id=?',
                              (watermark.get('agent_provider_requests',0),pid)).fetchone()
            provider_usage=dict(zip(('requests','measured_requests','input_tokens','cached_input_tokens','output_tokens'),counts))
            provider_usage['missing_usage_requests']=counts[0]-counts[1]
            provider_usage['cache_hit_fraction']=counts[3]/counts[2] if counts[2] else None
        batch_count = db.execute("SELECT COUNT(*) FROM events WHERE rowid>? AND kind='agent.inbox_batch_decided' "
                                 "AND json_extract(payload,'$.pipeline_id')=?", (watermark["events"], pid)).fetchone()[0]
        live = [dict(r) for r in db.execute("SELECT id,role,state,session_id,checkpoint,turn_active,updated_at "
                                            "FROM agent_workers WHERE pipeline_id=? AND (state='running' OR turn_active=1)", (pid,))]
    return {"observed_at_utc": datetime.now(timezone.utc).isoformat(), "baseline": str(folder),
            "pipeline": pipeline, "counts": new_counts,
            "delta": {key: new_counts[key] - old_counts[key] for key in new_counts},
            "worker_states": counts, "live_workers": live, "pending_inbox": pending,
            "new_submission_results": receipts, "human_approval_batches": batch_count,
            "quota_paused": json.loads(quota_row[0]).get("paused", False) if quota_row else False,
            "usage_delta": dict(zip(("turns", "input_tokens", "cached_input_tokens", "output_tokens"), usage)),
            "provider_response_delta":provider_usage,
            "timing": timing}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--record", action="store_true", help="Append a compact sample inside the experiment folder.")
    args = parser.parse_args()
    result = status()
    if args.record:
        with (Path(result["baseline"]) / "observations.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(result, ensure_ascii=False) + "\n")
    print(json.dumps(result, ensure_ascii=False))

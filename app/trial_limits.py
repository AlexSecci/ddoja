"""Persistent admission limits for supervised provider trials, not job decisions."""
import json
import time

PREFIX = "agent_trial:"


def read(db, pipeline_id):
    row = db.execute("SELECT value FROM meta WHERE key=?", (PREFIX + pipeline_id,)).fetchone()
    return json.loads(row[0]) if row else None


def runs(db, policy):
    return db.execute("SELECT r.*,w.role FROM agent_execution_runs r JOIN agent_workers w ON w.id=r.worker_id "
                      "WHERE w.pipeline_id=? AND r.rowid>? AND COALESCE(r.state,'')!='not_launched'", (policy["pipeline_id"], policy["execution_watermark"])).fetchall()


def allows(db, worker, desired, *, may_have_sent=False):
    policy = read(db, worker["pipeline_id"])
    if not policy:
        return True
    if policy["state"] != "active" or time.time() >= policy["deadline_unix"] or may_have_sent:
        return False
    if desired.get("provider") != policy["provider"] or desired.get("model") != policy["model"]:
        return False
    if worker["role"] != "discovery" and worker["job_id"] not in policy["job_ids"]:
        return False
    records = runs(db, policy)
    if len(records) >= policy["max_invocations"]:
        return False
    if worker["role"] == "discovery" and sum(r["role"] == "discovery" for r in records) >= policy["max_discovery_invocations"]:
        return False
    return True


def stop_reason(db, policy):
    if time.time() >= policy["deadline_unix"]:
        return "Supervised trial reached its wall-clock limit."
    active = db.execute("SELECT 1 FROM agent_workers WHERE pipeline_id=? AND turn_active=1 LIMIT 1", (policy["pipeline_id"],)).fetchone()
    if len(runs(db, policy)) >= policy["max_invocations"] and not active:
        return "Supervised trial reached its worker-invocation limit."
    placeholders = ','.join('?' for _ in policy["job_ids"])
    remaining = db.execute("SELECT 1 FROM jobs WHERE id IN (" + placeholders + ") AND phase NOT IN ('submitted','discarded') LIMIT 1", policy["job_ids"]).fetchone()
    if not remaining:
        return "All jobs selected for the supervised trial reached terminal results."
    return None

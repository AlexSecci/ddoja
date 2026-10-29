"""Persistent admission limits for supervised provider trials, not job decisions."""
import json
import time

PREFIX = "agent_trial:"


def expired(policy):
    deadline = policy.get('deadline_unix')
    return deadline is not None and time.time() >= deadline


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
    if policy["state"] != "active" or expired(policy):
        return False
    if policy.get('allowed_worker_ids') and worker['id'] not in policy['allowed_worker_ids']:
        return False
    if policy.get('allowed_roles') and worker['role'] not in policy['allowed_roles']:
        return False
    if may_have_sent:
        prior = json.loads(worker['context']).get('execution', {})
        current_trial_continuation = (worker['role']=='submission' and worker['job_id'] in policy['job_ids']
                                      and any(row['worker_id']==worker['id'] for row in runs(db,policy)))
        if not ((current_trial_continuation or policy.get('reconciliation_allowed') and worker['id'] in policy.get('allowed_worker_ids', []))
                and prior.get('provider') == policy['provider'] and prior.get('model') == policy['model']):
            return False
    if desired.get("provider") != policy["provider"] or desired.get("model") != policy["model"]:
        return False
    if worker["role"] != "discovery" and worker["job_id"] not in policy["job_ids"]:
        return False
    records = runs(db, policy)
    if policy.get('max_invocations') is not None and len(records) >= policy["max_invocations"]:
        return False
    if worker["role"] == "discovery" and sum(r["role"] == "discovery" for r in records) >= policy["max_discovery_invocations"]:
        return False
    return True


def stop_reason(db, policy):
    if expired(policy):
        return "Supervised trial reached its wall-clock limit."
    active = db.execute("SELECT 1 FROM agent_workers WHERE pipeline_id=? AND turn_active=1 LIMIT 1", (policy["pipeline_id"],)).fetchone()
    if policy.get('max_invocations') is not None and len(runs(db, policy)) >= policy["max_invocations"] and not active:
        return "Supervised trial reached its worker-invocation limit."
    placeholders = ','.join('?' for _ in policy["job_ids"])
    if set(policy.get('allowed_roles', [])) == {'evaluator'}:
        remaining = db.execute("SELECT 1 FROM pipeline_jobs pj JOIN jobs j ON j.id=pj.job_id "
            "WHERE pj.pipeline_id=? AND pj.job_id IN (" + placeholders + ") AND "
            "(pj.fit_score IS NULL OR pj.fit_jd_version!=j.jd_version OR pj.fit_profile_version!="
            "CAST((SELECT value FROM meta WHERE key='profile_version') AS INTEGER)) LIMIT 1",
            [policy["pipeline_id"], *policy["job_ids"]]).fetchone()
        return "All jobs selected for the evaluation-only batch have current fit scores." if not remaining and not active else None
    remaining = db.execute("SELECT 1 FROM jobs j LEFT JOIN pipeline_jobs pj ON pj.job_id=j.id AND pj.pipeline_id=? "
        "WHERE j.id IN (" + placeholders + ") AND j.phase NOT IN ('submitted','discarded') "
        "AND COALESCE(pj.agent_status,'') NOT IN ('submitted','discarded') LIMIT 1",
        [policy["pipeline_id"], *policy["job_ids"]]).fetchone()
    # A committed receipt precedes the same turn's safe tab cleanup and usage
    # report. Admission is already closed to other tasks; let that turn exit.
    if not remaining and not active:
        return "All jobs selected for the supervised trial reached terminal results."
    return None

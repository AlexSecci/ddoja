"""Cross-company job catalog and explicit human terminal decisions."""
from .store import Conflict, timestamp


STATES = {'all', 'scored', 'unscored', 'legacy', 'needs_rescoring', 'submitted',
          'discarded', 'uncertain', 'excluded', 'active', 'failed'}


def records(store, *, state='all', company_id=None, order='score_desc', query=''):
    if state not in STATES or order not in {'score_desc', 'score_asc', 'company'}:
        raise ValueError('Choose a supported job state and ordering.')
    with store.connect() as db:
        profile = store.profile_version(db)
        rows = db.execute("SELECT j.*,c.name AS company_name,o.status AS application_status,"
            "o.provenance AS application_provenance,pj.pipeline_id,pj.fit_score,pj.fit_jd_version,"
            "pj.fit_profile_version,pj.agent_decision,pj.agent_status,pj.manual_discard_reason,pj.selected,"
            "EXISTS(SELECT 1 FROM agent_submission_results r WHERE r.job_id=j.id AND r.state='submitted') "
            "OR EXISTS(SELECT 1 FROM evidence e WHERE e.job_id=j.id AND e.kind='receipt') AS has_submission_receipt,"
            "EXISTS(SELECT 1 FROM agent_workers w WHERE w.job_id=j.id AND w.turn_active=1) AS worker_active,"
            "EXISTS(SELECT 1 FROM agent_workers w WHERE w.job_id=j.id AND w.state IN ('failed','interrupted')) AS worker_failed,"
            "EXISTS(SELECT 1 FROM evaluations e WHERE e.job_id=j.id) AS legacy_evaluated "
            "FROM jobs j JOIN companies c ON c.id=j.company_id "
            "LEFT JOIN application_outcomes o ON o.job_id=j.id "
            "LEFT JOIN pipeline_jobs pj ON pj.rowid=(SELECT x.rowid FROM pipeline_jobs x WHERE x.job_id=j.id "
            "ORDER BY (x.fit_jd_version=(SELECT jd_version FROM jobs WHERE id=x.job_id) AND x.fit_profile_version=?) DESC,"
            "x.discovered_at DESC,x.rowid DESC LIMIT 1)", (profile,)).fetchall()
        result = []
        for raw in rows:
            job = dict(raw)
            job['fit_current'] = (job['fit_score'] is not None and job['fit_jd_version'] == job['jd_version']
                                  and job['fit_profile_version'] == profile)
            if not job['fit_current']:
                job['fit_score'] = None
            job['has_submission_receipt'] = bool(job['has_submission_receipt'])
            job['worker_active'] = bool(job['worker_active'] or job['active_stage'] or job['lease_token'])
            if job['manual_status']:
                status = job['manual_status']
            elif job['phase'] == 'submitted' or job['has_submission_receipt'] or job['agent_status'] == 'submitted':
                status = 'submitted'
            elif job['run_status'] == 'submission_uncertain' or job['agent_status'] == 'uncertain':
                status = 'uncertain'
            elif (job['phase'] == 'discarded' or job['manual_discard_reason']
                  or job['agent_decision'] == 'discard' or job['agent_status'] == 'discarded'):
                status = 'discarded'
            elif job['selected'] == 0:
                status = 'excluded'
            elif job['worker_active']:
                status = 'active'
            elif job['fit_current']:
                status = 'scored'
            elif job['fit_jd_version'] is not None:
                status = 'needs_rescoring'
            elif job['agent_decision'] or job['legacy_evaluated']:
                status = 'legacy'
            elif job['worker_failed']:
                status = 'failed'
            else:
                status = 'unscored'
            job['record_status'] = status
            job['manual_submitted_available'] = not job['worker_active'] and status != 'submitted'
            job['manual_discard_available'] = not job['worker_active'] and status not in {'submitted', 'discarded'}
            if company_id and job['company_id'] != company_id:
                continue
            if state == 'scored' and not job['fit_current']:
                continue
            if state == 'unscored' and job['fit_current']:
                continue
            if state not in {'all', 'scored', 'unscored'} and status != state:
                continue
            if query and query.casefold() not in ' '.join(str(job.get(k) or '') for k in
                    ('title', 'company_name', 'location', 'requisition_id')).casefold():
                continue
            result.append(job)
        def sort_key(job):
            score = job['fit_score']
            score_key = (score is None, (score if order == 'score_asc' else -score) if score is not None else 0)
            tie = (str(job['title']).casefold(), job['id'])
            return ((job['company_name'].casefold(), *score_key, *tie) if order == 'company'
                    else (*score_key, job['company_name'].casefold(), *tie))
        return sorted(result, key=sort_key)


def set_manual_status(store, job_id, status, note=''):
    if status not in {'submitted', 'discarded'} or not isinstance(note, str) or len(note) > 500:
        raise ValueError('Choose submitted or discarded and a note of at most 500 characters.')
    with store.connect(write=True) as db:
        job = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        if not job:
            raise KeyError(job_id)
        if job['manual_status'] == status:
            return {'job_id': job_id, 'status': status, 'manual': True}
        if job['active_stage'] or job['lease_token'] or db.execute(
                'SELECT 1 FROM agent_workers WHERE job_id=? AND turn_active=1', (job_id,)).fetchone():
            raise Conflict('A worker is active on this job. Stop it and wait for its checkpoint before changing the status.')
        if status == 'discarded' and (job['phase'] == 'submitted' or db.execute(
                "SELECT 1 FROM agent_submission_results WHERE job_id=? AND state='submitted'", (job_id,)).fetchone()
                or db.execute("SELECT 1 FROM evidence WHERE job_id=? AND kind='receipt'", (job_id,)).fetchone()):
            raise Conflict('This job is already submitted; its submission record must be preserved.')
        now = timestamp()
        reason = note.strip() or ('Already applied; confirmed by user.' if status == 'submitted' else 'Discarded by user.')
        db.execute("UPDATE jobs SET manual_status=?,manual_status_note=?,manual_status_at=?,phase=?,"
                   "run_status='idle',reason=? WHERE id=?", (status, reason, now, status, reason, job_id))
        db.execute("UPDATE pipeline_jobs SET agent_status=?,manual_discard_reason=?,manual_discard_provenance=?,"
                   "manual_discard_actor='user',manual_discarded_at=? WHERE job_id=?",
                   (status, reason if status == 'discarded' else '', 'Explicit dashboard decision', now, job_id))
        # Withdraw queued/waiting work, preserving historical sends and receipts.
        db.execute("UPDATE agent_workers SET state='stopped',error=?,capability_sha256='',capability_expires_at=NULL,"
                   "completed_at=COALESCE(completed_at,?),updated_at=? WHERE job_id=? AND turn_active=0 "
                   "AND state NOT IN ('completed','discarded','superseded','uncertain')", (reason, now, now, job_id))
        db.execute("UPDATE agent_inbox SET state='superseded',provenance=?,resolved_at=? WHERE job_id=? AND state='pending'",
                   ('User manually marked the job '+status+'. No browser approval granted.', now, job_id))
        db.execute("UPDATE agent_browser_controls SET state='closed',updated_at=?,reason=?,provenance=? "
                   "WHERE control_type='company_auth' AND state='open' AND worker_id IN "
                   "(SELECT id FROM agent_workers WHERE job_id=?)", (now,
                   'The user removed this job from automated work; authentication ownership was released.',
                   'Manual job status only; not evidence of sign-in or a browser-action approval.', job_id))
        store._agent_wake_orphaned_auth_waiters(db)
        store.event(db, 'job.manual_status', {'status': status, 'note': reason,
                    'previous_phase': job['phase'], 'previous_run_status': job['run_status'],
                    'receipt_verified': False}, job_id, 'user')
        for membership in db.execute('SELECT p.id,p.mode FROM pipelines p JOIN pipeline_jobs pj ON pj.pipeline_id=p.id WHERE pj.job_id=?', (job_id,)).fetchall():
            if membership['mode'] == 'agentic':
                store._agent_refresh_pipeline(db, membership['id'])
    return {'job_id': job_id, 'status': status, 'manual': True, 'note': reason, 'recorded_at': now}

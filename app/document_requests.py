"""Explicit document-only requests, isolated from application campaigns."""
import json
import uuid

from .store import Conflict, timestamp


def summaries(db, store):
    result = {}
    profile_version = store.profile_version(db)
    for row in db.execute("SELECT * FROM agent_workers WHERE role='document' AND parent_id IS NULL "
                          "AND json_extract(context,'$.document_request_owner')=1 ORDER BY rowid DESC"):
        context = store._agent_decode(row['context'], {})
        child = db.execute('SELECT id,state,error,jd_version,profile_version FROM agent_workers WHERE parent_id=? '
                           'ORDER BY rowid DESC LIMIT 1', (row['id'],)).fetchone()
        job = db.execute('SELECT jd_version FROM jobs WHERE id=?', (row['job_id'],)).fetchone()
        current = bool(job and child and child['jd_version'] == job['jd_version']
                       and child['profile_version'] == profile_version)
        if row['job_id'] not in result:
            result[row['job_id']] = {'id':row['id'], 'worker_id':child['id'] if child else None,
                'state':child['state'] if child else row['state'], 'error':child['error'] if child else '',
                'artifacts_requested':context.get('requested_artifacts', []), 'current':current, 'artifacts':[]}
        # Keep the latest ready file of each kind across explicit requests.
        # Writing a cover letter later must not hide an existing current résumé.
        if current:
            saved = result[row['job_id']]['artifacts']
            for artifact in context.get('ready_artifacts', []):
                if not any(a['kind'] == artifact['kind'] for a in saved):
                    saved.append(artifact)
    return result


def request(store, job_id, artifacts, execution=None):
    if not isinstance(artifacts, list) or not artifacts or any(k not in {'resume','cover_letter'} for k in artifacts):
        raise ValueError('Choose a résumé, cover letter, or both.')
    requested = sorted(set(artifacts))
    with store.connect(write=True) as db:
        record = next((j for j in store.job_records() if j['id'] == job_id), None)
        if record is None:
            raise KeyError(job_id)
        if record['record_status'] in {'submitted','discarded'}:
            raise Conflict('Documents can only be requested for jobs that are not submitted or discarded.')
        job = db.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()
        if (job['manual_status'] or job['phase'] in {'submitted','discarded'} or db.execute(
                "SELECT 1 FROM agent_submission_results WHERE job_id=? AND state='submitted'", (job_id,)).fetchone()
                or db.execute("SELECT 1 FROM evidence WHERE job_id=? AND kind='receipt'", (job_id,)).fetchone()):
            raise Conflict('The job was submitted or discarded; no document task was started.')
        if not db.execute('SELECT 1 FROM job_versions WHERE job_id=? AND version=? AND length(jd)>0',
                          (job_id,job['jd_version'])).fetchone():
            raise Conflict('Capture the complete job description before requesting documents.')
        quota = store._agent_decode(db.execute("SELECT value FROM meta WHERE key='agent_quota_latch'").fetchone()[0], {})
        if quota.get('paused'):
            raise Conflict('The pipeline is quota-paused. No document task was started.')
        old = summaries(db, store).get(job_id)
        if old and old['state'] in {'queued','running','waiting_child','waiting_question','waiting_approval','quota_paused'}:
            if old['artifacts_requested'] != requested:
                raise Conflict('A document request is already active for this job. Let it finish before changing the request.')
            return {**old,'created':False}
        override = None
        if execution is not None:
            from .worker_settings import read_settings, validate_roles
            roles = dict(read_settings(store.root,db)['roles']);roles['materials'] = execution
            override = validate_roles(store.root,roles)['materials']
        now = timestamp();pipeline_id = uuid.uuid4().hex
        db.execute("INSERT INTO pipelines(id,company_id,status,stage,filters,discovery_status,mode,dashboard_hidden,"
                   "discovery_paused,evaluation_paused,submission_paused,created_at,updated_at) "
                   "VALUES(?,?,'running','documents',?,'complete','agentic',1,1,1,1,?,?)",
                   (pipeline_id,job['company_id'],json.dumps({'document_only':True}),now,now))
        owner = store._agent_insert_worker(db,pipeline_id,'document',job_id=job_id,
            context={'document_request_owner':True,'document_only':True,'requested_artifacts':requested,
                     'document_request_reason':'Explicit user request for tailored files only. No application or upload.'})
        db.execute("UPDATE agent_workers SET state='waiting_child',checkpoint='documents_only' WHERE id=?",(owner,))
        context = {'requested_artifacts':requested,'document_only':True}
        if override:
            context['document_execution_override'] = override
        child = store._agent_insert_worker(db,pipeline_id,'document',job_id=job_id,parent_id=owner,context=context)
        store.event(db,'agent.document_only_requested',{'request_id':owner,'worker_id':child,
            'pipeline_id':pipeline_id,'artifacts':requested,'execution_override':override,
            'submission_authorized':False},job_id,'user')
        return {'id':owner,'worker_id':child,'pipeline_id':pipeline_id,'state':'queued',
                'artifacts_requested':requested,'created':True,'artifacts':[]}

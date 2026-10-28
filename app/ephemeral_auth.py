"""One-use, encrypted code handoffs; never applicant facts or inbox responses."""
import json
from pathlib import Path
import time
import uuid
import hashlib

from .provider_credentials import _dpapi

AUTH_ACTIONS={'enter_emailed_human_verification_code_to_submit','company_authentication'}
WAIT_CHECKPOINT='awaiting_email_token_handoff'


def authorization(root,worker_id):
    from .store import Store
    store=Store(root)
    with store.connect() as db:
        worker=db.execute('SELECT * FROM agent_workers WHERE id=?',(worker_id,)).fetchone()
        if not worker or worker['role'] not in {'submission','discovery'}:
            return None
        for row in db.execute("SELECT * FROM agent_inbox WHERE worker_id=? AND state='approved' ORDER BY resolved_at DESC",(worker_id,)):
            ctx=json.loads(row['context'])
            if row['action'] in AUTH_ACTIONS and ctx.get('jd_version')==worker['jd_version'] and ctx.get('profile_version')==worker['profile_version']:
                return {'worker_id':worker_id,'job_id':worker['job_id'],'inbox_id':row['id'],'scope_token':store._agent_approval_scope_token(dict(row)),'authorization_type':'exact_human_approval'}
        policy=db.execute("SELECT value FROM meta WHERE key='routine_email_tokens'").fetchone()
        value=json.loads(policy[0]) if policy else {}
        if value.get('enabled') and worker['role'] in value.get('roles',['discovery','submission']):
            scope=hashlib.sha256(json.dumps({'policy':value,'worker_id':worker_id,'job_id':worker['job_id']},sort_keys=True).encode()).hexdigest()
            return {'worker_id':worker_id,'job_id':worker['job_id'],'inbox_id':None,'scope_token':scope,'authorization_type':'standing_retrieval_policy'}
    return None


def hint(root,worker_id):
    grant=authorization(root,worker_id)
    if not grant:
        return None
    return {**grant,'automatic_retrieval':True,'checkpoint':WAIT_CHECKPOINT,
            'instruction':'When the current site actually sends an email token and shows its code field, call checkpoint with label awaiting_email_token_handoff. It waits briefly for a scoped, encrypted one-use handoff. If the result is pending, return disposition waiting; delivery wakes this same task with its page retained. Do not poll model turns, ask the human for the code, call Gmail MCP tools, or run a shell. Separate required legal/CAPTCHA approvals remain separate; the current exact approved email step needs no repeated request.'}


def wait_for_handoff(root,worker_id, *, wait_seconds=None):
    grant=authorization(root,worker_id)
    if not grant:
        raise ValueError('No scoped email-token retrieval authorization is available.')
    root=Path(root);folder=root/'data/auth-handoffs';folder.mkdir(parents=True,exist_ok=True)
    request_path=folder/(worker_id+'.request.json')
    previous=json.loads(request_path.read_text()) if request_path.exists() else {}
    reuse=(previous.get('scope_token')==grant['scope_token'] and not previous.get('delivered_at_unix') and not previous.get('cancelled_at_unix'))
    request=previous if reuse else {**grant,'request_id':uuid.uuid4().hex,'requested_at_unix':time.time()}
    temporary=request_path.with_suffix('.tmp')
    temporary.write_text(json.dumps(request),encoding='utf-8');temporary.replace(request_path)
    if wait_seconds is None:
        config=root/'config/submission-efficiency.json'
        settings=json.loads(config.read_text()) if config.exists() else {}
        wait_seconds=settings.get('email_handoff_wait_seconds',90)
    deadline=time.monotonic()+max(0,min(120,wait_seconds))
    try:
        import urllib.request
        cap=__import__('os').environ.get('AGENT_CAPABILITY')
        if cap:
            req=urllib.request.Request('http://127.0.0.1:8765/api/auth-handoffs/'+worker_id+'/wake',
                data=json.dumps({'request_id':request['request_id']}).encode(),method='POST',
                headers={'Content-Type':'application/json','x-worker-capability':cap})
            urllib.request.urlopen(req,timeout=2).close()
    except Exception:pass
    while True:
        value=consume(root,worker_id)
        if value:
            request.update(delivered_at_unix=time.time())
            request_path.write_text(json.dumps(request),encoding='utf-8')
            return value
        outcome_path=folder/(worker_id+'.outcome.json')
        if outcome_path.exists():
            outcome=json.loads(outcome_path.read_text());outcome_path.unlink(missing_ok=True)
            if outcome.get('request_id')==request['request_id'] and outcome.get('scope_token')==grant['scope_token']:
                request.update(cancelled_at_unix=time.time())
                request_path.write_text(json.dumps(request),encoding='utf-8')
                return {'recovery_required':True,'outcome':outcome['outcome'],
                        'instruction':'The scoped lookup did not find one fresh matching code. Reobserve the page. If the site offers resend, request one fresh code and checkpoint again. Do not loop resends or retry an uncertain final send. If this repeats, ask for attention and return waiting.'}
        import sqlite3
        with sqlite3.connect(root/'data/pipeline.sqlite') as db:
            state=db.execute('SELECT p.stop_requested,w.state FROM agent_workers w JOIN pipelines p ON p.id=w.pipeline_id WHERE w.id=?',(worker_id,)).fetchone()
        if not state or state[0] or state[1] in {'stopped','quota_paused','superseded','completed','discarded'}:
            return {'cancelled':True,'reason':'The owning task was stopped.'}
        if time.monotonic()>=deadline:
            from .store import Store
            store=Store(root)
            with store.connect(write=True) as db:
                db.execute("UPDATE agent_workers SET state='waiting_browser' WHERE id=? AND state='running'",(worker_id,))
                store.event(db,'agent.email_handoff_waiting',{'worker_id':worker_id,'request_id':request['request_id']},actor='auth_broker')
            return {'pending':True,'state':'waiting_browser','request_id':request['request_id'],
                    'instruction':'No fresh token was delivered yet. Return final JSON disposition waiting; the page and this exact request are retained. Delivery wakes this worker without polling model turns. Do not request another token or block indefinitely.'}
        time.sleep(.5)


def consume(root, worker_id):
    root = Path(root)
    path = root / 'data/auth-handoffs' / (worker_id + '.dpapi')
    if not path.is_file():
        return None
    from .store import Store
    store = Store(root)
    grant=authorization(root,worker_id)
    with store.connect() as db:
        value = json.loads(_dpapi(path.read_bytes(), decrypt=True))
        if value['worker_id'] != worker_id or value['expires_unix'] <= time.time():
            path.unlink(missing_ok=True)
            return None
        request_path=path.with_suffix('.request.json')
        request=json.loads(request_path.read_text(encoding='utf-8')) if request_path.exists() else {}
        if not value.get('request_id') or value['request_id']!=request.get('request_id'):
            path.unlink(missing_ok=True)
            return None
        item = db.execute('SELECT * FROM agent_inbox WHERE id=? AND worker_id=? AND job_id IS ? AND state=?',
                          (value['inbox_id'], worker_id, value['job_id'], 'approved')).fetchone()
        gate = db.execute("SELECT 1 FROM agent_browser_controls WHERE worker_id=? AND control_type='company_auth' AND state='open'", (worker_id,)).fetchone()
        worker = db.execute('SELECT w.jd_version,w.profile_version,p.stop_requested FROM agent_workers w JOIN pipelines p ON p.id=w.pipeline_id WHERE w.id=?', (worker_id,)).fetchone()
        context = json.loads(item['context']) if item else {}
        exact=bool(worker and item and item['action'] in AUTH_ACTIONS and store._agent_approval_scope_token(dict(item))==value['scope_token']
                   and context.get('jd_version')==worker['jd_version'] and context.get('profile_version')==worker['profile_version'])
        standing=bool(grant and grant['authorization_type']=='standing_retrieval_policy' and grant['scope_token']==value['scope_token'])
        if not gate or not worker or worker['stop_requested'] or not (exact or standing):
            return None
    path.unlink()
    metadata={k:value[k] for k in ('worker_id','job_id','inbox_id','scope_token','expires_unix','request_id')}
    path.with_suffix('.consumed.json').write_text(json.dumps(metadata),encoding='utf-8')
    return {'code': value['code'], 'job_id': value['job_id'], 'inbox_id': value['inbox_id'],
            'expires_unix': value['expires_unix'],
            'instruction': 'The supervisor retrieved this token under the saved human authorization. Reobserve the owning task and current verification challenge, then enter this one-use code. Do not open Gmail or quote/store the code/email body. Complete_auth only after verification succeeds. For submission, retain all separate actual-action approvals and call begin_final_action immediately before an authorized final application click; retrieval alone does not authorize a new send. An exact approved Greenhouse verification continuation may keep the owned auth gate open for code redaction until verification finishes. Take a fresh receipt observation before finish_submission. If the code is rejected/expired, request one fresh code for this same pending step and checkpoint for the supervisor. An uncertain send stays reconciliation-only; no blind resubmission.'}


def permitted_final(store,db,worker):
    path=Path(store.root)/'data/auth-handoffs'/(worker['id']+'.consumed.json')
    try:
        value=json.loads(path.read_text(encoding='utf-8'))
    except (OSError,ValueError):
        return False
    if value.get('worker_id')!=worker['id'] or value.get('job_id')!=worker['job_id']:
        return False
    request_path=path.parent/(worker['id']+'.request.json')
    if request_path.exists():
        request=json.loads(request_path.read_text())
        if request.get('cancelled_at_unix') or request.get('request_id')!=value.get('request_id'):return False
    item=db.execute("SELECT * FROM agent_inbox WHERE id=? AND worker_id=? AND job_id=? AND state='approved'",(value.get('inbox_id'),worker['id'],worker['job_id'])).fetchone()
    if not item:
        return False
    context=json.loads(item['context'])
    return (item['action'] in AUTH_ACTIONS
            and not store._agent_inbox_stale_reason(db,{**dict(item),'worker_jd_version':worker['jd_version'],'worker_profile_version':worker['profile_version']})
            and store._agent_approval_scope_token(dict(item))==value.get('scope_token')
            and context.get('jd_version')==worker['jd_version'] and context.get('profile_version')==worker['profile_version'])


def bind_human_decision(store,db,item):
    """Bind an already entered code to the actual new human decision, not to TTL."""
    if item['action'] not in AUTH_ACTIONS or item['state']!='approved':return False
    path=Path(store.root)/'data/auth-handoffs'/(item['worker_id']+'.consumed.json')
    try:value=json.loads(path.read_text())
    except (OSError,ValueError):return False
    if value.get('worker_id')!=item['worker_id'] or value.get('job_id')!=item['job_id']:return False
    request=path.parent/(item['worker_id']+'.request.json')
    if request.exists() and json.loads(request.read_text()).get('request_id')!=value.get('request_id'):return False
    value['retrieval_scope_token']=value['scope_token'];value['inbox_id']=item['id']
    value['scope_token']=store._agent_approval_scope_token(dict(item))
    temporary=path.with_suffix('.tmp');temporary.write_text(json.dumps(value),encoding='utf-8');temporary.replace(path)
    store.event(db,'agent.email_verification_scope_bound',{'worker_id':item['worker_id'],'inbox_id':item['id'],'request_id':value['request_id']},item['job_id'],'human_decision')
    return True


def final_scope(root,worker_id):
    try:
        value=json.loads((Path(root)/'data/auth-handoffs'/(worker_id+'.consumed.json')).read_text(encoding='utf-8'))
        return value.get('request_id')
    except (OSError,ValueError):
        return None

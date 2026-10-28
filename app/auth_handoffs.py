"""Validated secret delivery and non-secret recovery outcomes for one nonce."""
import json
from pathlib import Path
import time
import re
from .provider_credentials import _dpapi


def deliver(store,worker_id,message_id,code):
    if not isinstance(code,str) or not re.fullmatch('[A-Za-z0-9]{4,32}',code):
        raise ValueError('Invalid token format.')
    from .ephemeral_auth import authorization
    grant=authorization(store.root,worker_id)
    if not grant:raise ValueError('No current scoped token retrieval grant.')
    folder=Path(store.root)/'data/auth-handoffs';path=folder/(worker_id+'.request.json')
    request=json.loads(path.read_text())
    if request.get('delivered_at_unix') or request.get('cancelled_at_unix') or request.get('scope_token')!=grant['scope_token']:
        raise ValueError('The token request changed or is no longer pending.')
    with store.connect(write=True) as db:
        worker=db.execute('SELECT w.*,p.company_id,p.stop_requested FROM agent_workers w JOIN pipelines p ON p.id=w.pipeline_id WHERE w.id=?',(worker_id,)).fetchone()
        if not worker or worker['stop_requested'] or worker['state'] not in {'running','waiting_browser'} or worker['checkpoint']!='awaiting_email_token_handoff':
            raise ValueError('The owning task is not at its current token checkpoint.')
        gate=db.execute("SELECT * FROM agent_browser_controls WHERE scope_key=? AND state='open'",('company:'+worker['company_id'],)).fetchone()
        if gate and gate['worker_id']!=worker_id:raise ValueError('Another authentication flow owns the company gate.')
        evidence=store._agent_latest_cua_event(db,worker,success=True)
        job=db.execute('SELECT url FROM jobs WHERE id=?',(worker['job_id'],)).fetchone()
        if not evidence or not job or evidence.get('application_url')!=job[0]:raise ValueError('A current exact application observation is required.')
        store._agent_set_browser_control(db,'company:'+worker['company_id'],'company_auth','open',
            'Scoped encrypted token delivery.','Standing retrieval policy or exact human step approval',worker_id,[evidence['event_id']])
        store.event(db,'agent.email_token_delivered',{'worker_id':worker_id,'request_id':request['request_id'],
            'message_id':message_id,'authorization_type':grant['authorization_type']},worker['job_id'],'auth_broker')
    value={**grant,'request_id':request['request_id'],'code':code,'expires_unix':time.time()+300}
    dest=folder/(worker_id+'.dpapi');temp=dest.with_suffix('.tmp');temp.write_bytes(_dpapi(json.dumps(value).encode()));temp.replace(dest)
    wake(store,worker_id,request['request_id'])
    return {'delivered':True,'worker_id':worker_id,'request_id':request['request_id']}


def wake(store,worker_id,request_id):
    from .ephemeral_auth import authorization
    grant=authorization(store.root,worker_id)
    path=Path(store.root)/'data/auth-handoffs'/(worker_id+'.request.json')
    request=json.loads(path.read_text())
    if not grant or request.get('scope_token')!=grant['scope_token'] or request.get('request_id')!=request_id or request.get('cancelled_at_unix'):raise ValueError('Stale handoff nonce.')
    available=(path.parent/(worker_id+'.dpapi')).exists() or (path.parent/(worker_id+'.outcome.json')).exists()
    with store.connect(write=True) as db:
        db.execute("UPDATE agent_workers SET state='queued',completed_at=NULL WHERE id=? AND state='waiting_browser' AND checkpoint='awaiting_email_token_handoff' AND ? AND EXISTS(SELECT 1 FROM pipelines p WHERE p.id=agent_workers.pipeline_id AND p.stop_requested=0)",(worker_id,int(available)))


def outcome(store,worker_id,request_id,reason):
    if reason not in {'no_fresh_mail','ambiguous_mail','broker_unavailable'}:raise ValueError('Unknown handoff outcome.')
    path=Path(store.root)/'data/auth-handoffs'/(worker_id+'.request.json');request=json.loads(path.read_text())
    if request['request_id']!=request_id or request.get('delivered_at_unix'):raise ValueError('Stale handoff outcome.')
    value={'request_id':request_id,'scope_token':request['scope_token'],'outcome':reason}
    dest=path.parent/(worker_id+'.outcome.json');tmp=dest.with_suffix('.tmp');tmp.write_text(json.dumps(value));tmp.replace(dest)
    wake(store,worker_id,request_id)

"""Deliver one current authorized email token without logging its value."""
import argparse
import getpass
import json
from pathlib import Path
import re
import sys
import time
import warnings

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from app.store import Store
from app.provider_credentials import _dpapi
from app.ephemeral_auth import authorization


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--worker',required=True)
    parser.add_argument('--message-id',required=True)
    args=parser.parse_args()
    store=Store(ROOT);grant=authorization(ROOT,args.worker)
    if not grant:
        raise ValueError('No current retrieval authorization for this worker.')
    folder=ROOT/'data/auth-handoffs';request_path=folder/(args.worker+'.request.json')
    request=json.loads(request_path.read_text(encoding='utf-8'))
    if request.get('delivered_at_unix') or request.get('scope_token')!=grant['scope_token']:
        raise ValueError('The handoff request is already served or its authorization changed.')
    with store.connect() as db:
        worker=dict(db.execute('SELECT w.*,p.company_id,p.stop_requested FROM agent_workers w JOIN pipelines p ON p.id=w.pipeline_id WHERE w.id=?',(args.worker,)).fetchone())
        if worker['stop_requested'] or worker['state']!='running' or worker['checkpoint']!='awaiting_email_token_handoff':
            raise ValueError('The owning task is not waiting in its live email-code checkpoint.')
    warnings.simplefilter('error',getpass.GetPassWarning)
    code=getpass.getpass('Current email token (hidden): ').strip()
    if not re.fullmatch('[A-Za-z0-9]{4,32}',code):
        raise ValueError('Invalid token format.')
    with store.connect(write=True) as db:
        gate=db.execute("SELECT * FROM agent_browser_controls WHERE scope_key=? AND state='open'",('company:'+worker['company_id'],)).fetchone()
        if gate and gate['worker_id']!=args.worker:
            raise ValueError('Another authentication flow owns this company gate.')
        evidence=store._agent_latest_cua_event(db,worker,success=True)
        if not evidence:
            raise ValueError('A current browser observation is required.')
        store._agent_set_browser_control(db,'company:'+worker['company_id'],'company_auth','open',
            'Automatic scoped email-token delivery under the user instruction; secret transport is redacted.',
            'Standing user policy plus current step authorization',args.worker,[evidence['event_id']])
        store.event(db,'agent.email_token_delivered',{'worker_id':args.worker,'request_id':request['request_id'],'message_id':args.message_id,'authorization_type':grant['authorization_type']},worker['job_id'],'supervisor')
    value={**grant,'request_id':request['request_id'],'code':code,'expires_unix':time.time()+300}
    path=folder/(args.worker+'.dpapi');temporary=path.with_suffix('.tmp')
    temporary.write_bytes(_dpapi(json.dumps(value).encode()));temporary.replace(path)
    print(json.dumps({'delivered':True,'worker_id':args.worker,'request_id':request['request_id']}))


if __name__=='__main__':main()

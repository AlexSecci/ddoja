import base64
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import threading
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from app import ephemeral_auth
from app.agent_runtime import CodexAgentRuntime
from app.auth_broker import AuthBroker,extract_code
from app.auth_handoffs import deliver,outcome
from app.context_budget import rotate
from app.efficiency_features import features
from app.provider_telemetry import ProviderTelemetry,request_shape
from app.request_metrics import RequestMetrics,effective_rows
from app.server import create_app
from app.store import Conflict
from app.worker_contracts import capture,load
from test_email_handoff import fixture
from test_email_handoff import deliver as fixture_deliver

ROOT=Path(__file__).resolve().parents[1]


def observation(s,j,wid,invalid=False):
    text='### Page\n- Page URL: '+j['url']+'\n### Snapshot\n- group "enter the 8-character code to confirm":\n'
    text+='\n'.join('- textbox [ref=code%d]: A'%i for i in range(8))
    if invalid:text+='\n- paragraph: Invalid security code'
    text+='\n- button "Submit application" '+('[disabled] ' if invalid else '')+'[ref=submit]'
    return s.agent_runtime_event(wid,{'type':'tool_result','tool_name':'playwright.browser_snapshot','success':True,
        'session_id':'fixture-session','transport_event':{'type':'item.completed','item':{
        'type':'mcp_tool_call','server':'playwright','tool':'browser_snapshot','status':'completed',
        'result':{'content':[{'type':'text','text':text}]}}}})


def request(s,wid):
    folder=s.root/'data/auth-handoffs';folder.mkdir(parents=True,exist_ok=True)
    value={**ephemeral_auth.authorization(s.root,wid),'request_id':'fresh-nonce','requested_at_unix':time.time()}
    (folder/(wid+'.request.json')).write_text(json.dumps(value));return value


def test_contract_is_role_specific_immutable_and_new_workers_get_changes(tmp_path):
    shutil.copytree(ROOT/'prompts',tmp_path/'prompts')
    w={'id':'test-worker','role':'evaluator','context':{'execution':{'browser_backend':'playwright'}}}
    first=capture(tmp_path,w,features(tmp_path))
    with (tmp_path/'prompts/submission_playwright.md').open('a') as f:f.write('\nUnrelated edit')
    assert capture(tmp_path,w,features(tmp_path))['id']==first['id']
    with (tmp_path/'prompts/evaluation_agent.md').open('a') as f:f.write('\nRelevant edit')
    assert capture(tmp_path,{**w,'id':'new-worker'},features(tmp_path))['id']!=first['id']
    assert load(tmp_path,w['id'],first['id'])==first
    path=tmp_path/'data/worker-contracts'/w['id']/(first['id']+'.json')
    corrupt=copy.deepcopy(first);corrupt['prompts']['role']='Changed';path.write_text(json.dumps(corrupt))
    with pytest.raises(ValueError,match='changed on disk'):load(tmp_path,w['id'],first['id'])


@pytest.mark.parametrize('stream',[True,False])
def test_relay_persists_actual_usage_and_canonicalizes_without_logging_content(tmp_path,stream):
    received=[]
    usage={'input_tokens':1000,'input_tokens_details':{'cached_tokens':900},'output_tokens':20,'output_tokens_details':{'reasoning_tokens':5}}
    class Upstream(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            received.append(self.rfile.read(int(self.headers['Content-Length'])))
            data={'id':'secret-response-id','status':'completed','usage':usage}
            body=('event: response.completed\r\ndata: '+json.dumps({'type':'response.completed','response':data})+'\r\n\r\n').encode() if stream else json.dumps(data).encode()
            self.send_response(200);self.send_header('Content-Type','text/event-stream' if stream else 'application/json');self.send_header('Content-Length',str(len(body)));self.end_headers()
            # Exercise fragmented SSE JSON and CRLF frames.
            for i in range(0,len(body),13):self.wfile.write(body[i:i+13]);self.wfile.flush()
    server=ThreadingHTTPServer(('127.0.0.1',0),Upstream)
    thread=threading.Thread(target=lambda:server.serve_forever(poll_interval=.02),daemon=True);thread.start()
    relay=ProviderTelemetry(tmp_path,'test-worker','test-run',{'provider':'deepseek'},'fictional-private-key',upstream=f'http://127.0.0.1:{server.server_port}')
    try:
        with httpx.Client(timeout=3,trust_env=False) as client:
            assert client.post(relay.url+'/responses',json={}).status_code==403
            payload={'stream':stream,'model':'fictional','instructions':'private applicant facts','input':[{'role':'user','content':'private prompt'}],
                     'tools':[{'type':'function','name':'z'},{'name':'a','type':'function'}]}
            response=client.post(relay.url+'/responses',json=payload,headers={'Authorization':'Bearer fictional-private-key'})
            assert response.status_code==200
            assert b'"name":"a"' in received[0] and received[0].index(b'"name":"a"')<received[0].index(b'"name":"z"')
            deadline=time.monotonic()+1
            while time.monotonic()<deadline:
                import sqlite3
                with sqlite3.connect(relay.metrics.path) as db:row=db.execute('SELECT input_tokens,cached_input_tokens,output_tokens,metadata FROM agent_provider_requests').fetchone()
                if row[0] is not None:break
                time.sleep(.01)
            assert row[:3]==(1000,900,20)
            for secret in ('private applicant facts','private prompt','fictional-private-key','secret-response-id'):assert secret not in row[3]
    finally:relay.close();server.shutdown();server.server_close();thread.join(1)


def test_prefix_fingerprints_distinguish_configuration_and_input_changes():
    body={'model':'fictional','instructions':'same','input':[{'role':'user','content':'one'}]}
    a,hashes=request_shape(body)
    b,_=request_shape({**body,'input':body['input']+[{'role':'assistant','content':'two'}]},hashes)
    assert a['configuration_sha256']==b['configuration_sha256'] and b['common_prefix_items_with_previous']==1
    c,_=request_shape({**body,'instructions':'changed'},hashes)
    assert c['instructions_sha256']!=a['instructions_sha256']


def test_provider_rows_replace_cli_counters_and_mark_partial_lower_bound(tmp_path,monkeypatch):
    s,p,j,wid=fixture(tmp_path,monkeypatch)
    s.record_agent_usage(wid,{'session_id':'fixture-session','turn_id':'test-run','counter_kind':'turn','counters':{'input_tokens':1000,'output_tokens':20,'cached_input_tokens':900}})
    metrics=RequestMetrics(s.root)
    metrics.record('one',wid,'test-run',{},time.time(),'completed',{}, {'input_tokens':1000,'input_tokens_details':{'cached_tokens':900},'output_tokens':20})
    metrics.record('two',wid,'test-run',{},time.time(),'failed',{})
    with s.connect() as db:
        rows=effective_rows(db,db.execute('SELECT w.role,u.* FROM agent_usage u JOIN agent_workers w ON w.id=u.worker_id').fetchall())
    assert len(rows)==1 and rows[0]['delta_input_tokens']==1000 and rows[0]['partial_usage']
    report=s.get_agent_efficiency(p['id'])
    assert report['totals']['input_tokens']==1000 and report['totals']['token_totals_are_lower_bounds']


@pytest.mark.parametrize('possibly_sent',[False,True])
def test_context_rotation_preserves_browser_scope_and_never_rotates_uncertain_send(tmp_path,monkeypatch,possibly_sent):
    s,p,j,wid=fixture(tmp_path,monkeypatch)
    context={'execution':{'browser_backend':'playwright'},'final_action_may_have_been_sent':possibly_sent,
             'browser':{'tab':'owned-tab'},'context_handoff_pending':True}
    with s.connect(write=True) as db:
        db.execute('UPDATE agent_workers SET context=? WHERE id=?',(json.dumps(context),wid))
        row=db.execute('SELECT w.*,p.company_id FROM agent_workers w JOIN pipelines p ON p.id=w.pipeline_id WHERE w.id=?',(wid,)).fetchone()
        changed=rotate(s,db,row)
        assert changed['company_id']==p['company_id']
    w=s.get_agent_worker(wid)
    assert w['context']['browser']==context['browser']
    if possibly_sent:assert w['session_id']=='fixture-session'
    else:assert w['session_id'] is None and w['context']['context_handoffs'][0]['session_id']=='fixture-session'


def test_bounded_email_wait_reuses_nonce_and_delivery_wakes_same_worker(tmp_path,monkeypatch):
    s,p,j,wid=fixture(tmp_path,monkeypatch);observation(s,j,wid)
    with s.connect(write=True) as db:db.execute("UPDATE agent_workers SET checkpoint='awaiting_email_token_handoff' WHERE id=?",(wid,))
    result=ephemeral_auth.wait_for_handoff(s.root,wid,wait_seconds=0)
    assert result['pending'] and s.get_agent_worker(wid)['state']=='waiting_browser'
    first=result['request_id']
    assert ephemeral_auth.wait_for_handoff(s.root,wid,wait_seconds=0)['request_id']==first
    monkeypatch.setattr('app.auth_handoffs._dpapi',lambda b,**kw:b[::-1])
    deliver(s,wid,'fictional-message','Ab1Cd2Ef')
    assert s.get_agent_worker(wid)['state']=='queued'
    assert ephemeral_auth.consume(s.root,wid)['code']=='Ab1Cd2Ef'


def test_nonsecret_no_mail_outcome_ends_wait_and_retires_nonce(tmp_path,monkeypatch):
    s,p,j,wid=fixture(tmp_path,monkeypatch);value=request(s,wid)
    outcome(s,wid,value['request_id'],'no_fresh_mail')
    result=ephemeral_auth.wait_for_handoff(s.root,wid,wait_seconds=0)
    assert result['recovery_required'] and result['outcome']=='no_fresh_mail'
    assert json.loads((s.root/'data/auth-handoffs'/(wid+'.request.json')).read_text())['cancelled_at_unix']


def test_automatic_verification_inbox_requests_human_without_granting_consent(tmp_path,monkeypatch):
    s,p,j,wid=fixture(tmp_path,monkeypatch);observation(s,j,wid)
    with s.connect(write=True) as db:
        db.execute("UPDATE agent_inbox SET state='rejected' WHERE worker_id=?",(wid,))
        row=db.execute('SELECT w.*,p.company_id,p.stop_requested FROM agent_workers w JOIN pipelines p ON p.id=w.pipeline_id WHERE w.id=?',(wid,)).fetchone()
        result=s._agent_begin_final_action(db,row,{})
        assert not result['authorized'] and result['state']=='waiting_approval'
        item=db.execute('SELECT * FROM agent_inbox WHERE id=?',(result['inbox_item_id'],)).fetchone()
        assert item['state']=='pending' and json.loads(item['context'])['source_event_id']
        assert not ephemeral_auth.permitted_final(s,db,row)


def test_stopped_worker_cannot_create_automatic_verification_request(tmp_path,monkeypatch):
    s,p,j,wid=fixture(tmp_path,monkeypatch);observation(s,j,wid)
    with s.connect(write=True) as db:
        db.execute('UPDATE pipelines SET stop_requested=1 WHERE id=?',(p['id'],))
        row=db.execute('SELECT w.*,p.company_id,p.stop_requested FROM agent_workers w JOIN pipelines p ON p.id=w.pipeline_id WHERE w.id=?',(wid,)).fetchone()
        count=db.execute('SELECT count(*) FROM agent_inbox').fetchone()[0]
        with pytest.raises(Conflict,match='stop'):s._agent_begin_final_action(db,row,{})
        assert db.execute('SELECT count(*) FROM agent_inbox').fetchone()[0]==count


def test_code_parser_requires_single_semantic_or_unambiguous_code():
    def mail(text):return {'mimeType':'text/plain','body':{'data':base64.urlsafe_b64encode(text.encode()).decode().rstrip('=')}}
    assert extract_code(mail('Your security code is: Ab1Cd2Ef'))=='Ab1Cd2Ef'
    assert extract_code(mail('Ab1Cd2Ef and Cd3Ef4Gh')) is None


def test_broker_fallback_and_endpoint_reject_unauthenticated_wake(tmp_path):
    class Manager:
        def __init__(self,store):pass
        def recover_interrupted(self):pass
        def shutdown(self):pass
        def schedule(self):pass
    app=create_app(tmp_path,agent_manager_factory=Manager,runtime_factory=lambda:None)
    with TestClient(app) as client:
        token=client.get('/api/session').json()['token']
        assert client.get('/api/settings/gmail-broker',headers={'x-pipeline-token':token}).json()['configured'] is False
        assert client.post('/api/auth-handoffs/unknown/wake',json={'request_id':'nonce'}).status_code==403
        assert client.post('/api/auth-handoffs/unknown/delivered',json={'request_id':'nonce'}).status_code==403
        assert app.state.auth_broker.wake('unknown','nonce')['mode']=='codex_connector_fallback'


def test_actual_human_decision_binds_consumed_code_to_current_step_only(tmp_path,monkeypatch):
    s,p,j,wid=fixture(tmp_path,monkeypatch)
    with s.connect(write=True) as db:db.execute("UPDATE agent_inbox SET state='rejected' WHERE worker_id=?",(wid,))
    value=request(s,wid);fixture_deliver(s,wid,nonce=value['request_id']);ephemeral_auth.consume(s.root,wid)
    observation(s,j,wid)
    with s.connect(write=True) as db:
        row=db.execute('SELECT w.*,p.company_id,p.stop_requested FROM agent_workers w JOIN pipelines p ON p.id=w.pipeline_id WHERE w.id=?',(wid,)).fetchone()
        pending=s._agent_begin_final_action(db,row,{})
        item=db.execute('SELECT i.*,w.session_id,w.role FROM agent_inbox i JOIN agent_workers w ON w.id=i.worker_id WHERE i.id=?',(pending['inbox_item_id'],)).fetchone()
        s._agent_record_inbox_decision(db,item,'approve','Synthetic explicit human decision','2026-10-28T12:00:00Z')
        row=db.execute('SELECT * FROM agent_workers WHERE id=?',(wid,)).fetchone()
        assert ephemeral_auth.permitted_final(s,db,row)
    path=s.root/'data/auth-handoffs'/(wid+'.request.json')
    current=json.loads(path.read_text());current['request_id']='different-future-challenge';path.write_text(json.dumps(current))
    with s.connect() as db:
        row=db.execute('SELECT * FROM agent_workers WHERE id=?',(wid,)).fetchone()
        assert not ephemeral_auth.permitted_final(s,db,row)


@pytest.mark.parametrize('user_stopped',[False,True])
def test_verified_rejection_recovery_is_bounded_and_respects_user_stop(tmp_path,monkeypatch,user_stopped):
    s,p,j,wid=fixture(tmp_path,monkeypatch);observation(s,j,wid,invalid=True)
    request(s,wid)
    with s.connect(write=True) as db:
        db.execute("UPDATE pipelines SET status='running',stop_requested=? WHERE id=?",(int(user_stopped),p['id']))
        db.execute("UPDATE agent_workers SET state='uncertain',turn_active=0,context=? WHERE id=?",(json.dumps({'final_action_may_have_been_sent':True}),wid))
    assert s.recover_current_code_rejection(wid) is not user_stopped
    row=s.get_agent_worker(wid)
    if user_stopped:assert row['state']=='uncertain' and row['context']['final_action_may_have_been_sent']
    else:
        assert row['state']=='queued' and row['session_id']=='fixture-session'
        assert row['context']['verified_code_rejections'][0]['prior_send_may_have_occurred']
        assert not s.recover_current_code_rejection(wid)


def test_committed_receipt_is_idempotent_and_cannot_be_downgraded(tmp_path,monkeypatch):
    s,p,j,wid=fixture(tmp_path,monkeypatch);observation(s,j,wid)
    with s.connect(write=True) as db:
        db.execute('UPDATE agent_workers SET context=? WHERE id=?',(json.dumps({'final_action_may_have_been_sent':True}),wid))
        row=db.execute('SELECT * FROM agent_workers WHERE id=?',(wid,)).fetchone()
        first=s._agent_finish_submission(db,row,{'status':'submitted','detail':'Fictional receipt test'})
        second=s._agent_finish_submission(db,row,{'status':'failed','detail':'Later transport error'})
        assert second['already_committed'] and second['result_id']==first['result_id']
        assert db.execute('SELECT count(*) FROM agent_submission_results WHERE worker_id=?',(wid,)).fetchone()[0]==1
    s.finish_agent_turn(wid,{'disposition':'failed','error':'Synthetic post-receipt cleanup error'})
    assert s.get_agent_worker(wid)['state']=='completed' and s.get_job(j['id'])['phase']=='submitted'
    with s.connect() as db:
        assert db.execute("SELECT state FROM agent_browser_controls WHERE worker_id=?",(wid,)).fetchone()[0]=='closed'


def test_startup_releases_only_inactive_auth_owner_with_a_real_receipt(tmp_path,monkeypatch):
    s,p,j,wid=fixture(tmp_path,monkeypatch)
    assert s.release_completed_auth_controls()==0
    with s.connect(write=True) as db:
        db.execute("UPDATE agent_workers SET state='completed',turn_active=0 WHERE id=?",(wid,))
    assert s.release_completed_auth_controls()==0
    with s.connect(write=True) as db:
        db.execute("INSERT INTO agent_submission_results(id,pipeline_id,worker_id,job_id,state,detail,observations,event_ids,created_at) VALUES('fictional-receipt',?,?,?,'submitted','Synthetic receipt','{}','[]','2026-10-28')",(p['id'],wid,j['id']))
    assert s.release_completed_auth_controls()==1
    assert s.release_completed_auth_controls()==0


def test_concurrent_provider_endpoints_are_per_worker_not_shared_runtime_state(tmp_path):
    (tmp_path/'config').mkdir();shutil.copy(ROOT/'config/workers.json',tmp_path/'config/workers.json')
    runtime=CodexAgentRuntime(tmp_path,codex_bin='fictional')
    base={'role':'submission','context':{'execution':{'provider':'deepseek','model':'deepseek-flash','effort':'low','browser_backend':'none'}}}
    a=runtime._args({**base,'id':'first','_provider_base_url':'http://127.0.0.1:1001/first'},tmp_path/'schema',{})
    b=runtime._args({**base,'id':'second','_provider_base_url':'http://127.0.0.1:1002/second'},tmp_path/'schema',{})
    assert any('1001/first' in arg for arg in a) and not any('1002/second' in arg for arg in a)
    assert any('1002/second' in arg for arg in b) and not any('1001/first' in arg for arg in b)

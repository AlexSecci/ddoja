import json
import threading
import time
from pathlib import Path

import pytest

from app import ephemeral_auth
from app.store import Store,Conflict


def fixture(tmp_path,monkeypatch):
    monkeypatch.setattr(ephemeral_auth,'_dpapi',lambda data,decrypt=False:data[::-1])
    s=Store(tmp_path);p=s.create_agent_pipeline('Fictional','https://job-boards.greenhouse.io/fictional')
    j=s.discover(p['company_id'],{'url':'https://job-boards.greenhouse.io/fictional/jobs/123','requisition_id':'123','title':'Fictional software role','jd':'Fictional full description.','country':'US','employment':'full_time','arrangement':'hybrid','location':'Austin'})
    j=s.get_job(j['id'])
    s.add_pipeline_job(p['id'],j['id'])
    with s.connect(write=True) as db:
        wid=s._agent_insert_worker(db,p['id'],'submission',job_id=j['id'])
        w=db.execute('SELECT * FROM agent_workers WHERE id=?',(wid,)).fetchone()
        ctx={'jd_version':w['jd_version'],'profile_version':w['profile_version'],'job_url':j['url']}
        item,_=s._agent_insert_inbox(db,w,'approval',action='enter_emailed_human_verification_code_to_submit',summary='Synthetic current email-code approval',context=ctx)
        db.execute("UPDATE agent_inbox SET state='approved',response='\"approve\"' WHERE id=?",(item['id'],))
        s._agent_set_browser_control(db,'company:'+p['company_id'],'company_auth','open','Fictional auth','Synthetic user decision',wid,[])
        db.execute("INSERT INTO meta(key,value) VALUES('routine_email_tokens',?)",(json.dumps({'enabled':True}),))
        db.execute("UPDATE agent_workers SET state='running',turn_active=1,session_id='fixture-session' WHERE id=?",(wid,))
    return s,p,j,wid


def deliver(s,wid,*,nonce='fixture-nonce',expired=False):
    grant=ephemeral_auth.authorization(s.root,wid)
    folder=s.root/'data/auth-handoffs';folder.mkdir(parents=True,exist_ok=True)
    req=folder/(wid+'.request.json')
    if not req.exists():req.write_text(json.dumps({'request_id':nonce}))
    value={**grant,'code':'Ab1Cd2Ef','request_id':nonce,'expires_unix':time.time()+(-1 if expired else 120)}
    (folder/(wid+'.dpapi')).write_bytes(json.dumps(value).encode()[::-1])


def test_code_is_one_use_encrypted_bound_to_approval_and_current_nonce(tmp_path,monkeypatch):
    s,_,_,wid=fixture(tmp_path,monkeypatch);deliver(s,wid)
    result=ephemeral_auth.consume(s.root,wid)
    assert result['code']=='Ab1Cd2Ef'
    folder=s.root/'data/auth-handoffs'
    assert not (folder/(wid+'.dpapi')).exists()
    assert 'Ab1Cd2Ef' not in (folder/(wid+'.consumed.json')).read_text()
    assert ephemeral_auth.consume(s.root,wid) is None
    with s.connect() as db:
        w=db.execute('SELECT * FROM agent_workers WHERE id=?',(wid,)).fetchone()
        assert ephemeral_auth.permitted_final(s,db,w)
    with s.connect(write=True) as db:db.execute('UPDATE agent_workers SET profile_version=profile_version+1 WHERE id=?',(wid,))
    with s.connect() as db:
        w=db.execute('SELECT * FROM agent_workers WHERE id=?',(wid,)).fetchone()
        assert not ephemeral_auth.permitted_final(s,db,w)


@pytest.mark.parametrize('blocked', [None, 'uncertain', 'running', 'stopped_pipeline', 'stale_profile', 'missing_challenge'])
def test_interrupted_empty_challenge_recovery_preserves_send_and_approval_scope(tmp_path, monkeypatch, blocked):
    s, p, j, wid = fixture(tmp_path, monkeypatch)
    text = ('### Page\n- Page URL: '+j['url']+'\n### Snapshot\n'
            '- group "enter the 8-character code to confirm":\n'
            + '\n'.join('- textbox [ref=code%d]' % n for n in range(8))
            + '\n- button "Submit application" [disabled] [ref=submit]')
    if blocked != 'missing_challenge':
        s.agent_runtime_event(wid, {'type': 'tool_result', 'tool_name': 'playwright.browser_snapshot',
            'success': True, 'session_id': 'fixture-session', 'transport_event': {'type': 'item.completed', 'item': {
                'type': 'mcp_tool_call', 'server': 'playwright', 'tool': 'browser_snapshot', 'status': 'completed',
                'result': {'content': [{'type': 'text', 'text': text}]}}}})
    with s.connect(write=True) as db:
        db.execute("UPDATE pipelines SET status='running' WHERE id=?", (p['id'],))
        db.execute("UPDATE agent_workers SET state=?,turn_active=0,started_at='2026-01-01',context=? WHERE id=?",
                   (blocked if blocked in {'uncertain', 'running'} else 'interrupted',
                    json.dumps({'final_action_may_have_been_sent': True}), wid))
        if blocked == 'stopped_pipeline': db.execute('UPDATE pipelines SET stop_requested=1 WHERE id=?', (p['id'],))
        if blocked == 'stale_profile': db.execute('UPDATE agent_workers SET profile_version=profile_version+1 WHERE id=?', (wid,))
    if blocked:
        with pytest.raises(Conflict): s.recover_pending_email_verification(wid, 'Verified operator recovery')
    else:
        result = s.recover_pending_email_verification(wid, 'Verified operator recovery')
        assert result['state'] == 'queued' and result['session_id'] == 'fixture-session'
        assert result['context']['final_action_may_have_been_sent'] is True
        assert not result['context'].get('email_verification_final_scope')
        with s.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM agent_inbox WHERE worker_id=? AND state='approved'", (wid,)).fetchone()[0] == 1


@pytest.mark.parametrize('invalid_code', [False, True])
def test_recovery_requires_explicit_current_code_rejection_and_preserves_history(tmp_path, monkeypatch, invalid_code):
    s, p, j, wid = fixture(tmp_path, monkeypatch)
    boxes = '\n'.join('- textbox [ref=code%d]: A' % n for n in range(8))
    error = '\n- paragraph [ref=error]: Invalid security code' if invalid_code else ''
    text = ('### Page\n- Page URL: ' + j['url'] + '\n### Snapshot\n'
            '- group "enter the 8-character code to confirm":\n' + boxes + error
            + '\n- button "Submit application" [disabled] [ref=submit]')
    event = {'type': 'tool_result', 'tool_name': 'playwright.browser_snapshot', 'success': True,
             'session_id': 'fixture-session', 'transport_event': {'type': 'item.completed', 'item': {
                 'type': 'mcp_tool_call', 'server': 'playwright', 'tool': 'browser_snapshot',
                 'status': 'completed', 'result': {'content': [{'type': 'text', 'text': text}]}}}}
    event_id = s.agent_runtime_event(wid, event)
    with s.connect(write=True) as db:
        w = db.execute('SELECT context FROM agent_workers WHERE id=?', (wid,)).fetchone()
        context = json.loads(w['context']);context['final_action_may_have_been_sent'] = True
        db.execute("UPDATE agent_workers SET state='stopped',turn_active=0,context=? WHERE id=?", (json.dumps(context), wid))
        db.execute("UPDATE jobs SET run_status='submission_uncertain' WHERE id=?", (j['id'],))
    with pytest.raises(Conflict, match='running worker'):
        s.agent_runtime_event(wid, event)
    event['supervisor_read_only_observation'] = True
    event_id = s.agent_runtime_event(wid, event)
    if not invalid_code:
        with pytest.raises(Conflict, match='fresh exact-page'):
            s.reconcile_rejected_greenhouse_code(wid, event_id)
        assert s.get_agent_worker(wid)['context']['final_action_may_have_been_sent']
    else:
        recovered = s.reconcile_rejected_greenhouse_code(wid, event_id)
        assert recovered['state'] == 'stopped' and recovered['session_id'] == 'fixture-session'
        assert recovered['context']['final_action_may_have_been_sent'] is False
        assert recovered['context']['verified_code_rejections'][0]['prior_send_may_have_occurred']
        assert s.get_job(j['id'])['run_status'] == 'idle'
        with pytest.raises(Conflict, match='already granted'):
            s.reconcile_rejected_greenhouse_code(wid, event_id)


@pytest.mark.parametrize('expired,wrong_nonce',[(True,False),(False,True)])
def test_expired_or_previous_request_token_is_not_disclosed(tmp_path,monkeypatch,expired,wrong_nonce):
    s,_,_,wid=fixture(tmp_path,monkeypatch)
    deliver(s,wid,expired=expired)
    if wrong_nonce:(s.root/'data/auth-handoffs'/(wid+'.request.json')).write_text(json.dumps({'request_id':'new-current-request'}))
    assert ephemeral_auth.consume(s.root,wid) is None


def test_code_wait_keeps_same_task_running_without_another_model_turn(tmp_path,monkeypatch):
    s,_,_,wid=fixture(tmp_path,monkeypatch);result=[]
    thread=threading.Thread(target=lambda:result.append(ephemeral_auth.wait_for_handoff(s.root,wid)))
    thread.start();req=s.root/'data/auth-handoffs'/(wid+'.request.json')
    deadline=time.monotonic()+3
    while not req.exists() and time.monotonic()<deadline:time.sleep(.02)
    request=json.loads(req.read_text());deliver(s,wid,nonce=request['request_id'])
    thread.join(3)
    assert not thread.is_alive() and result[0]['code']=='Ab1Cd2Ef'
    assert s.get_agent_worker(wid)['state']=='running'


@pytest.mark.parametrize('role',['discovery','submission'])
def test_standing_policy_delivers_current_token_without_inventing_final_approval(tmp_path,monkeypatch,role):
    s,_,_,wid=fixture(tmp_path,monkeypatch)
    with s.connect(write=True) as db:
        db.execute("UPDATE agent_inbox SET state='rejected' WHERE worker_id=?",(wid,))
        db.execute('UPDATE agent_workers SET role=? WHERE id=?',(role,wid))
    assert ephemeral_auth.hint(s.root,wid)['automatic_retrieval']
    assert ephemeral_auth.authorization(s.root,wid)['authorization_type']=='standing_retrieval_policy'
    deliver(s,wid)
    assert ephemeral_auth.consume(s.root,wid)['code']=='Ab1Cd2Ef'
    with s.connect() as db:
        w=db.execute('SELECT * FROM agent_workers WHERE id=?',(wid,)).fetchone()
        assert not ephemeral_auth.permitted_final(s,db,w)


def test_verification_proof_redacts_digits_and_allows_only_one_final_continuation(tmp_path,monkeypatch):
    s,p,j,wid=fixture(tmp_path,monkeypatch);deliver(s,wid);ephemeral_auth.consume(s.root,wid)
    fields='\n'.join('    - textbox'+(' "Security code"' if i==0 else '')+f' [ref=e{i}]: '+('"'+ch+'"' if ch.isdigit() else ch) for i,ch in enumerate('Ab1Cd2Ef'))
    text='### Page\n- Page URL: '+j['url']+'\n### Snapshot\n```yaml\n- group "enter the 8-character code to confirm you are human":\n'+fields+'\n    - button "Submit application" [ref=e9]\n```'
    event={'type':'tool_result','tool_name':'playwright.browser_snapshot','success':True,'session_id':'fixture-session','transport_event':{'type':'item.completed','item':{'type':'mcp_tool_call','server':'playwright','tool':'browser_snapshot','status':'completed','result':{'content':[{'type':'text','text':text}]}}}}
    s.agent_runtime_event(wid,event)
    with s.connect(write=True) as db:
        payload=json.loads(db.execute('SELECT payload FROM agent_runtime_events WHERE worker_id=? ORDER BY rowid DESC LIMIT 1',(wid,)).fetchone()[0])
        assert payload['authentication_redacted'] and payload['verification_observation']['filled_boxes']==8
        assert 'textbox' not in json.dumps(payload)
        w=db.execute('SELECT w.*,p.company_id,p.stop_requested FROM agent_workers w JOIN pipelines p ON p.id=w.pipeline_id WHERE w.id=?',(wid,)).fetchone()
        review,_=s._agent_insert_inbox(db,w,'first_application_review',summary='Synthetic first application review',context={'jd_version':w['jd_version'],'profile_version':w['profile_version'],'ready_artifacts':[]})
        db.execute("UPDATE agent_inbox SET state='approved' WHERE id=?",(review['id'],))
        ctx=json.loads(w['context']);ctx['final_action_may_have_been_sent']=True
        db.execute("UPDATE agent_workers SET context=?,state='running' WHERE id=?",(json.dumps(ctx),wid))
        w=db.execute('SELECT w.*,p.company_id,p.stop_requested FROM agent_workers w JOIN pipelines p ON p.id=w.pipeline_id WHERE w.id=?',(wid,)).fetchone()
        assert s._agent_begin_final_action(db,w,{})['authorized']
        w=db.execute('SELECT w.*,p.company_id,p.stop_requested FROM agent_workers w JOIN pipelines p ON p.id=w.pipeline_id WHERE w.id=?',(wid,)).fetchone()
        with pytest.raises(Conflict,match='may already have started'):s._agent_begin_final_action(db,w,{})
    # Completing account freshness must not expose code boxes still visible on
    # the form; the subsequent genuine receipt remains available as evidence.
    with s.connect(write=True) as db:
        db.execute("UPDATE agent_browser_controls SET state='closed' WHERE worker_id=?",(wid,))
    event_id=s.agent_runtime_event(wid,event)
    with s.connect() as db:
        payload=json.loads(db.execute('SELECT payload FROM agent_runtime_events WHERE id=?',(event_id,)).fetchone()[0])
        assert payload['authentication_redacted'] and 'textbox' not in json.dumps(payload)
    receipt=json.loads(json.dumps(event))
    receipt['transport_event']['item']['result']['content'][0]['text']='### Page\n- Page URL: '+j['url']+'/confirmation\n### Snapshot\nThank you for applying. Your application has been received.'
    receipt_id=s.agent_runtime_event(wid,receipt)
    with s.connect() as db:
        payload=json.loads(db.execute('SELECT payload FROM agent_runtime_events WHERE id=?',(receipt_id,)).fetchone()[0])
        assert not payload.get('authentication_redacted') and 'has been received' in json.dumps(payload)

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

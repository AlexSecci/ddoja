import json

import pytest

from app.store import Conflict, timestamp
from test_email_handoff import fixture


def prepare(tmp_path, monkeypatch, *, intervening_input=False, verified_send=False):
    s, p, j, wid = fixture(tmp_path, monkeypatch)
    def observe(text, *, tool='browser_snapshot', args=None):
        return s.agent_runtime_event(wid, {'type':'tool_result','tool_name':'playwright.'+tool,
            'success':True,'session_id':'fixture-session','transport_event':{'type':'item.completed','item':{
                'type':'mcp_tool_call','server':'playwright','tool':tool,'status':'completed',
                'arguments':args or {},'result':{'content':[{'type':'text','text':text}]}}}})
    prefix='### Page\n- Page URL: '+j['url']+'\n### Snapshot\n'
    challenge=prefix+'- group "enter the 8-character code to confirm":\n'+('\n'.join(
        '- textbox [ref=code%d]' % n for n in range(8)))+'\n- button "Submit application" [disabled] [ref=submit]'
    with s.connect(write=True) as db:
        db.execute("UPDATE pipelines SET status='running' WHERE id=?",(p['id'],))
        db.execute("UPDATE agent_browser_controls SET state='closed'")
        db.execute("UPDATE agent_workers SET started_at=?,context=? WHERE id=?",(timestamp(),json.dumps({
            'final_action_may_have_been_sent':True,'final_action_authorized_at':timestamp(),
            **({'email_verification_final_scope':'previous-final'} if verified_send else {})}),wid))
        w=db.execute('SELECT * FROM agent_workers WHERE id=?',(wid,)).fetchone()
        review,_=s._agent_insert_inbox(db,w,'first_application_review',summary='Synthetic first review',
            context={'jd_version':w['jd_version'],'profile_version':w['profile_version'],'ready_artifacts':[]})
        db.execute("UPDATE agent_inbox SET state='approved' WHERE id=?",(review['id'],))
    observe(prefix+'- button "Submit application" [ref=submit]',tool='browser_click',args={'element':'Submit application button'})
    observe(challenge)
    if intervening_input:
        observe(challenge,tool='browser_click',args={'element':'code box'})
        observe(challenge)
    with s.connect(write=True) as db:db.execute("UPDATE agent_workers SET state='interrupted',turn_active=0 WHERE id=?",(wid,))
    return s,p,j,wid,observe,prefix,challenge


@pytest.mark.parametrize('input_after,verified_send',[(True,False),(False,True)])
def test_later_browser_input_or_verified_send_prevents_initial_challenge_refresh(tmp_path,monkeypatch,input_after,verified_send):
    s,_,_,wid,*_=prepare(tmp_path,monkeypatch,intervening_input=input_after,verified_send=verified_send)
    with pytest.raises(Conflict):s.recover_pending_email_verification(wid,'Scoped recovery',True)
    assert s.get_agent_worker(wid)['context']['final_action_may_have_been_sent'] is True


@pytest.mark.parametrize('form',['enabled','redacted','disabled','partial','wrong_url','challenge','receipt'])
def test_refresh_requires_new_full_exact_enabled_form_and_is_single_use(tmp_path,monkeypatch,form):
    s,p,j,wid,observe,prefix,challenge=prepare(tmp_path,monkeypatch)
    recovered=s.recover_pending_email_verification(wid,'Scoped recovery',True)
    assert recovered['context']['final_action_may_have_been_sent'] is True
    with s.connect(write=True) as db:db.execute("UPDATE agent_workers SET state='running',turn_active=1 WHERE id=?",(wid,))
    text=prefix+'- button "Submit application" '+('[disabled] ' if form=='disabled' else '')+'[ref=submit]'
    if form=='wrong_url':text=text.replace(j['url'],'https://unrelated.example/')
    if form=='challenge':text=challenge
    if form=='receipt':text+='\nThank you for applying. Your application has been received.'
    if form=='redacted':
        with s.connect(write=True) as db:s._agent_set_browser_control(db,'company:'+p['company_id'],'company_auth','open','Fixture auth','Fixture',wid,[])
    eid=observe(text,args={'depth':1} if form=='partial' else {})
    with s.connect(write=True) as db:
        w=db.execute('SELECT w.*,p.company_id,p.stop_requested FROM agent_workers w JOIN pipelines p ON p.id=w.pipeline_id WHERE w.id=?',(wid,)).fetchone()
        if form=='redacted':
            payload=json.loads(db.execute('SELECT payload FROM agent_runtime_events WHERE id=?',(eid,)).fetchone()[0])
            assert payload['authentication_redacted'] and payload['preverification_form_observation']=={'full_snapshot':True,'submit_enabled':True}
            assert 'Snapshot' not in json.dumps(payload)
            s._agent_browser_control_action(db,w,{'operation':'complete_auth','reason':'Fixture exact form restored'})
        if form not in {'enabled','redacted'}:
            with pytest.raises(Conflict):s._agent_begin_final_action(db,w,{})
        else:
            assert s._agent_begin_final_action(db,w,{})['authorized'] is True
            w=db.execute('SELECT w.*,p.company_id,p.stop_requested FROM agent_workers w JOIN pipelines p ON p.id=w.pipeline_id WHERE w.id=?',(wid,)).fetchone()
            ctx=json.loads(w['context'])
            assert ctx['final_action_may_have_been_sent'] is True and ctx['preverification_refresh']['consumed'] is True
            with pytest.raises(Conflict):s._agent_begin_final_action(db,w,{})


@pytest.mark.parametrize('blocked',[None,'submit','new_challenge','consumed','stopped'])
def test_reserved_form_recovery_cannot_replay_a_consumed_or_possible_send(tmp_path,monkeypatch,blocked):
    s,p,j,wid,observe,prefix,challenge=prepare(tmp_path,monkeypatch)
    s.recover_pending_email_verification(wid,'Scoped refresh',True)
    with s.connect(write=True) as db:db.execute("UPDATE agent_workers SET state='running',turn_active=1 WHERE id=?",(wid,))
    if blocked=='submit':observe(prefix,tool='browser_click',args={'element':'Submit application'})
    if blocked=='new_challenge':observe(challenge)
    observe(prefix+'- button "Submit application" [ref=submit]')
    with s.connect(write=True) as db:
        db.execute("UPDATE agent_workers SET state='uncertain',turn_active=0 WHERE id=?",(wid,))
        if blocked=='consumed':
            c=json.loads(db.execute('SELECT context FROM agent_workers WHERE id=?',(wid,)).fetchone()[0]);c['preverification_refresh']['consumed']=True
            db.execute('UPDATE agent_workers SET context=? WHERE id=?',(json.dumps(c),wid))
        if blocked=='stopped':db.execute('UPDATE pipelines SET stop_requested=1 WHERE id=?',(p['id'],))
    if blocked:
        with pytest.raises(Conflict):s.recover_reserved_preverification_form(wid,'Reviewed logging failure')
    else:
        w=s.recover_reserved_preverification_form(wid,'Reviewed logging failure')
        assert w['state']=='queued' and w['context']['final_action_may_have_been_sent'] is True
        assert w['context']['preverification_refresh']['consumed'] is False
        with pytest.raises(Conflict):s.recover_reserved_preverification_form(wid,'No duplicate recovery')

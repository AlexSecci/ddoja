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


@pytest.mark.parametrize('form',['enabled','disabled','partial','wrong_url','challenge','receipt'])
def test_refresh_requires_new_full_exact_enabled_form_and_is_single_use(tmp_path,monkeypatch,form):
    s,p,j,wid,observe,prefix,challenge=prepare(tmp_path,monkeypatch)
    recovered=s.recover_pending_email_verification(wid,'Scoped recovery',True)
    assert recovered['context']['final_action_may_have_been_sent'] is True
    with s.connect(write=True) as db:db.execute("UPDATE agent_workers SET state='running',turn_active=1 WHERE id=?",(wid,))
    text=prefix+'- button "Submit application" '+('[disabled] ' if form=='disabled' else '')+'[ref=submit]'
    if form=='wrong_url':text=text.replace(j['url'],'https://unrelated.example/')
    if form=='challenge':text=challenge
    if form=='receipt':text+='\nThank you for applying. Your application has been received.'
    observe(text,args={'depth':1} if form=='partial' else {})
    with s.connect(write=True) as db:
        w=db.execute('SELECT w.*,p.company_id,p.stop_requested FROM agent_workers w JOIN pipelines p ON p.id=w.pipeline_id WHERE w.id=?',(wid,)).fetchone()
        if form!='enabled':
            with pytest.raises(Conflict):s._agent_begin_final_action(db,w,{})
        else:
            assert s._agent_begin_final_action(db,w,{})['authorized'] is True
            w=db.execute('SELECT w.*,p.company_id,p.stop_requested FROM agent_workers w JOIN pipelines p ON p.id=w.pipeline_id WHERE w.id=?',(wid,)).fetchone()
            ctx=json.loads(w['context'])
            assert ctx['final_action_may_have_been_sent'] is True and ctx['preverification_refresh']['consumed'] is True
            with pytest.raises(Conflict):s._agent_begin_final_action(db,w,{})

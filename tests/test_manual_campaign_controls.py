import json

import pytest

from app.store import Conflict
from test_efficiency_controls import seed


def stopped_case(tmp_path, *, ended_trial=True):
    s,p,d,_,jobs=seed(tmp_path,1)
    s.finish_agent_turn(d['id'],{'disposition':'interrupted','checkpoint':'Saved discovery cursor'})
    policy=s.configure_agent_trial(p['id'],[jobs[0][0]['job_id']],seconds=None,max_invocations=None)
    with s.connect(write=True) as db:
        parent=s._agent_insert_worker(db,p['id'],'submission',job_id=jobs[0][0]['job_id'],
            context={'requested_artifacts':['resume']})
        child=s._agent_insert_worker(db,p['id'],'document',job_id=jobs[0][0]['job_id'],parent_id=parent)
        uncertain=s._agent_insert_worker(db,p['id'],'submission',job_id=jobs[0][0]['job_id'],
            context={'final_action_may_have_been_sent':True})
        db.execute("UPDATE agent_workers SET state='uncertain' WHERE id=?",(uncertain,))
        db.execute("UPDATE agent_workers SET started_at='fixture-start' WHERE id IN (?,?)",(parent,child))
    s.request_agent_pipeline_stop(p['id'])
    if ended_trial:s.close_agent_trial(p['id'],'The supervised application batch finished.')
    return s,p,d,parent,child,uncertain,policy


@pytest.mark.parametrize('stage,roles',[
    ('discovery',{'discovery'}),('evaluation',{'evaluator'}),('submission',{'submission','document'})])
def test_stopped_stage_start_archives_old_trial_and_resumes_only_that_stage(tmp_path,stage,roles):
    s,p,d,parent,child,uncertain,_=stopped_case(tmp_path)
    old_session=s.get_agent_worker(d['id'])['session_id']
    before_ids={w['id'] for w in s.list_agent_workers(p['id'])}
    with s.connect() as db:before_runs=db.execute('SELECT COUNT(*) FROM agent_execution_runs').fetchone()[0]
    view=s.resume_agent_pipeline(p['id'],manual_start=True,stage=stage)
    assert not view['stop_requested']
    assert all(view[name+'_paused']==(name!=stage) for name in ('discovery','evaluation','submission'))
    workers=s.list_agent_workers(p['id'])
    assert {w['id'] for w in workers}==before_ids
    ready={w['role'] for w in workers if w['state'] in {'queued','waiting_child'}}
    assert ready==roles and not any(w['turn_active'] for w in workers)
    assert s.get_agent_worker(uncertain)['state']=='uncertain'
    assert s.get_agent_worker(uncertain)['context']['final_action_may_have_been_sent']
    assert s.get_agent_trial(p['id']) is None
    if stage=='discovery':assert s.get_agent_worker(d['id'])['session_id']==old_session
    with s.connect() as db:
        histories=[json.loads(r[0]) for r in db.execute("SELECT value FROM meta WHERE key LIKE ?",('agent_trial_history:'+p['id']+':%',))]
        assert len(histories)==1 and histories[0]['state']=='ended'
        assert histories[0]['stop_reason']=='The supervised application batch finished.'
        assert db.execute('SELECT COUNT(*) FROM agent_execution_runs').fetchone()[0]==before_runs


def test_internal_trial_resume_stays_blocked_without_explicit_manual_start(tmp_path):
    s,p,*_=stopped_case(tmp_path)
    with pytest.raises(Conflict,match='bounded trial ended'):s.resume_agent_pipeline(p['id'])
    assert s.get_pipeline(p['id'])['stop_requested'] and s.get_agent_trial(p['id'])['state']=='ended'


def test_manual_campaign_start_respects_existing_stage_pauses(tmp_path):
    s,p,_,parent,child,_,_=stopped_case(tmp_path)
    s.set_stage_dispatch(p['id'],'evaluation',True)
    s.set_stage_dispatch(p['id'],'submission',True)
    view=s.resume_agent_pipeline(p['id'],manual_start=True)
    assert not view['stop_requested'] and not view['discovery_paused']
    assert view['evaluation_paused'] and view['submission_paused']
    assert s.get_agent_worker(parent)['state']=='stopped' and s.get_agent_worker(child)['state']=='stopped'
    assert {s.get_agent_worker(w)['role'] for w in s.list_queued_agent_workers()}=={'discovery'}


def test_all_paused_manual_start_explains_that_a_stage_must_be_enabled(tmp_path):
    s,p,*_=stopped_case(tmp_path)
    for stage in ('discovery','evaluation','submission'):s.set_stage_dispatch(p['id'],stage,True)
    view=s.resume_agent_pipeline(p['id'],manual_start=True)
    assert view['stage']=='stages_paused' and view['status']=='awaiting_user'
    assert s.list_queued_agent_workers()==[]


def test_quota_pause_prevents_trial_retirement_or_stage_resume(tmp_path):
    s,p,*_=stopped_case(tmp_path)
    s.set_agent_quota(True,reason='Fixture quota pause')
    before=s.get_pipeline(p['id'])
    with pytest.raises(Conflict,match='quota'):s.resume_agent_pipeline(p['id'],manual_start=True,stage='discovery')
    after=s.get_pipeline(p['id'])
    assert after['stop_requested'] and after['discovery_paused']==before['discovery_paused']
    assert s.get_agent_trial(p['id'])['state']=='ended' and s.get_agent_quota()['paused']
    with s.connect() as db:assert not db.execute("SELECT 1 FROM meta WHERE key LIKE 'agent_trial_history:%'").fetchone()


def test_stage_start_cannot_escape_an_active_bounded_run(tmp_path):
    s,p,*_=stopped_case(tmp_path,ended_trial=False)
    with s.connect(write=True) as db:
        key='agent_trial:'+p['id'];policy=json.loads(db.execute('SELECT value FROM meta WHERE key=?',(key,)).fetchone()[0])
        policy['allowed_roles']=['submission','document'];policy['max_discovery_invocations']=0
        db.execute('UPDATE meta SET value=? WHERE key=?',(json.dumps(policy),key))
    with pytest.raises(Conflict,match='active bounded'):s.resume_agent_pipeline(p['id'],manual_start=True,stage='discovery')
    assert s.get_agent_trial(p['id'])['state']=='active' and s.get_pipeline(p['id'])['stop_requested']


def test_stage_start_while_running_keeps_other_enabled_stages(tmp_path):
    s,p,*_=stopped_case(tmp_path)
    s.resume_agent_pipeline(p['id'],manual_start=True)
    view=s.resume_agent_pipeline(p['id'],manual_start=True,stage='evaluation')
    assert not any(view[name+'_paused'] for name in ('discovery','evaluation','submission'))


def test_explicit_discovery_start_after_completed_coverage_creates_one_new_pass(tmp_path):
    s,p,d,*_=stopped_case(tmp_path)
    with s.connect(write=True) as db:
        db.execute("UPDATE pipelines SET discovery_status='complete' WHERE id=?",(p['id'],))
        db.execute("UPDATE agent_workers SET state='completed' WHERE id=?",(d['id'],))
    s.resume_agent_pipeline(p['id'],manual_start=True,stage='discovery')
    workers=[w for w in s.list_agent_workers(p['id']) if w['role']=='discovery']
    assert len(workers)==2 and len([w for w in workers if w['state']=='queued'])==1
    assert s.get_agent_worker(d['id'])['state']=='completed'
    assert s.get_pipeline(p['id'])['discovery_status']=='pending'


@pytest.mark.parametrize('route,body',[
    ('dispatch',{'stage':'discovery','paused':False}),('resume',{})])
def test_dashboard_manual_start_routes_use_explicit_start_with_no_real_runtime(tmp_path,route,body):
    from test_ui_contract import _client
    s,p,*_=stopped_case(tmp_path)
    app,client,headers,managers=_client(tmp_path)
    response=client.post('/api/pipelines/'+p['id']+'/'+route,headers=headers,json=body)
    assert response.status_code==200,response.text
    assert not response.json()['pipeline']['stop_requested']
    assert app.state.store.get_agent_trial(p['id']) is None
    assert managers['instance'].schedule_calls==1
    assert not any(w['turn_active'] for w in app.state.store.list_agent_workers(p['id']))


def test_loading_dashboard_or_pausing_stage_keeps_trial_and_campaign_stopped(tmp_path):
    from test_ui_contract import _client
    s,p,*_=stopped_case(tmp_path)
    app,client,headers,_=_client(tmp_path)
    assert client.get('/').status_code==200
    response=client.post('/api/pipelines/'+p['id']+'/dispatch',headers=headers,json={'stage':'discovery','paused':True})
    assert response.status_code==200
    assert app.state.store.get_pipeline(p['id'])['stop_requested']
    assert app.state.store.get_agent_trial(p['id'])['state']=='ended'


def test_manual_start_during_cooperative_stop_waits_for_the_live_turn(tmp_path):
    s,p,d,*_=stopped_case(tmp_path)
    with s.connect(write=True) as db:
        db.execute("UPDATE pipelines SET status='stopping' WHERE id=?",(p['id'],))
        db.execute("UPDATE agent_workers SET state='running',turn_active=1 WHERE id=?",(d['id'],))
    with pytest.raises(Conflict,match='finish stopping'):
        s.resume_agent_pipeline(p['id'],manual_start=True,stage='discovery')
    assert s.get_pipeline(p['id'])['stop_requested']
    assert s.get_agent_trial(p['id'])['state']=='ended'

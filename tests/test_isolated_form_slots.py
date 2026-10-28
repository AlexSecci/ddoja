import json
import pytest

from app.store import Conflict
from test_efficiency_controls import seed,start


def prepared(tmp_path,backend='playwright',enabled=True):
    s,p,d,_,_=seed(tmp_path,4)
    for e in [w for w in s.list_agent_workers(p['id']) if w['role']=='evaluator']:
        launch=start(s,e)
        s.agent_action(e['id'],launch['capability'],'finish_evaluation',{'decision':'approve','reason':'Fictional controlled fixture'})
        s.finish_agent_turn(e['id'],{'disposition':'completed'})
    settings=s.get_worker_settings();roles=settings['roles']
    roles['submission']={'provider':'openai','model':'gpt-6-luna','effort':'medium','browser_backend':backend}
    s.save_worker_settings(settings['revision'],roles)
    (tmp_path/'config').mkdir(exist_ok=True)
    (tmp_path/'config/workers.json').write_text(json.dumps({'efficiency':{'max_concurrent_form_writers':1}}))
    (tmp_path/'config/efficiency.json').write_text(json.dumps({'isolated_form_slots':enabled}))
    return s,p,[w for w in s.list_agent_workers(p['id']) if w['role']=='submission']


def test_isolated_profiles_use_existing_three_slots_without_raising_caps(tmp_path):
    s,p,workers=prepared(tmp_path)
    assert all(s.reserve_agent_worker(w['id']) for w in workers[:3])
    assert not s.reserve_agent_worker(workers[3]['id'])


@pytest.mark.parametrize('backend,enabled',[('cua',True),('playwright',False)])
def test_native_shared_session_and_ablation_keep_single_form_writer(tmp_path,backend,enabled):
    s,p,workers=prepared(tmp_path,backend,enabled)
    assert s.reserve_agent_worker(workers[0]['id'])
    assert not s.reserve_agent_worker(workers[1]['id'])


def test_isolated_preparation_does_not_steal_or_bypass_email_owner(tmp_path):
    s,p,workers=prepared(tmp_path)
    assert s.reserve_agent_worker(workers[0]['id'])
    with s.connect(write=True) as db:
        s._agent_set_browser_control(db,'company:'+p['company_id'],'company_auth','open','Fictional email verification','fixture',workers[0]['id'],[])
    assert s.reserve_agent_worker(workers[1]['id'])
    with s.connect(write=True) as db:
        gate=db.execute("SELECT worker_id FROM agent_browser_controls WHERE control_type='company_auth'").fetchone()
        assert gate[0]==workers[0]['id']
        w=db.execute('SELECT w.*,p.company_id,p.stop_requested FROM agent_workers w JOIN pipelines p ON p.id=w.pipeline_id WHERE w.id=?',(workers[1]['id'],)).fetchone()
        with pytest.raises(Conflict,match='Authentication or connector recovery'):
            s._agent_begin_final_action(db,w,{})

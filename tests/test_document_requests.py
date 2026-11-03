import pytest

from app.document_requests import request
from app.store import Conflict
from test_efficiency_controls import seed, start
from tools.agent_mcp import AgentMCPBridge, render_and_register_documents


DS = {'provider':'deepseek','model':'deepseek-flash','effort':'high',
      'browser_backend':'none','service_tier':'default'}


def test_document_only_request_is_independent_deduplicated_and_pins_explicit_provider(tmp_path):
    s,p,_,_,entries=seed(tmp_path,1);job=entries[0][0]['job_id']
    s.request_agent_pipeline_stop(p['id'])
    controls=s.get_pipeline(p['id']);settings=s.get_worker_settings()['roles']
    queued=request(s,job,['resume'],DS)
    assert request(s,job,['resume'],DS)['id']==queued['id']
    with pytest.raises(Conflict,match='already active'):
        request(s,job,['cover_letter'])
    writer=s.get_agent_worker(queued['worker_id']);start(s,writer)
    assert s.get_agent_worker(writer['id'])['context']['execution']['provider']=='deepseek'
    assert s.get_agent_worker(writer['id'])['context']['execution']['effort']=='high'
    assert s.get_worker_settings()['roles']==settings
    assert {w['role'] for w in s.list_agent_workers(queued['pipeline_id'])}=={'document'}
    for name in ['status','stop_requested','discovery_paused','evaluation_paused','submission_paused']:
        assert s.get_pipeline(p['id'])[name]==controls[name]
    with pytest.raises(Conflict,match='document-only'):
        s.resume_agent_pipeline(queued['pipeline_id'],manual_start=True,stage='submission')
    assert s.prepare_ranked_submissions()==[]


@pytest.mark.parametrize('status',['submitted','discarded'])
def test_terminal_records_reject_document_requests(tmp_path,status):
    s,_,_,_,entries=seed(tmp_path,1);job=entries[0][0]['job_id']
    s.set_manual_job_status(job,status)
    assert not next(j for j in s.job_records() if j['id']==job)['document_generation_available']
    with pytest.raises(Conflict,match='not submitted or discarded'):
        request(s,job,['resume'])


def test_document_only_render_completes_without_queuing_any_submission(tmp_path):
    s,p,_,_,entries=seed(tmp_path,1);job=entries[0][0]['job_id']
    queued=request(s,job,['resume','cover_letter']);writer=s.get_agent_worker(queued['worker_id']);launch=start(s,writer)
    with pytest.raises(Conflict,match='not allowed'):
        s.agent_action(writer['id'],launch['capability'],'begin_final_action',{})
    bridge=AgentMCPBridge(writer['id'],launch['capability'],'document',s.agent_action,
        document_renderer=lambda published:render_and_register_documents(s,writer['id'],launch['capability'],published))
    value,images=bridge.invoke('publish_documents',{'artifacts':[
        {'kind':kind,'markdown':'# Fictional Candidate\n\nSoftware experience documented in this synthetic test.\n',
         'source_notes':{'fixture':True}} for kind in ['resume','cover_letter']]})
    assert images
    s.finish_agent_turn(writer['id'],{'disposition':'completed','session_id':'fixture-documents'})
    assert s.get_agent_worker(queued['id'])['state']=='completed'
    assert s.get_pipeline(queued['pipeline_id'])['status']=='completed'
    record=next(j for j in s.job_records() if j['id']==job)
    assert len(record['document_request']['artifacts'])==2
    assert record['pipeline_id']==p['id']  # No new catalog membership/scoring.
    for artifact in record['document_request']['artifacts']:
        assert s.get_agent_artifact_variant(artifact['id'],'pdf')['path']
    assert not any(w['role']=='submission' for w in s.list_agent_workers(queued['pipeline_id']))
    # A later request keeps current downloads available while it runs.
    later=request(s,job,['cover_letter'])
    record=next(j for j in s.job_records() if j['id']==job)
    assert record['document_request']['id']==later['id']
    assert {a['kind'] for a in record['document_request']['artifacts']}=={'resume','cover_letter'}


def test_document_request_route_validates_scopes_and_uses_authenticated_api(tmp_path):
    from test_ui_contract import _client
    s,_,_,_,entries=seed(tmp_path,1);job=entries[0][0]['job_id']
    app,client,headers,_=_client(tmp_path)
    endpoint='/api/jobs/'+job+'/documents'
    assert client.post(endpoint,json={'artifacts':['resume']}).status_code==403
    assert client.post(endpoint,json={'artifacts':['invalid']},headers=headers).status_code==422
    response=client.post(endpoint,json={'artifacts':['resume']},headers=headers)
    assert response.status_code==200,response.text
    assert app.state.agent_pipeline_manager.schedule_calls==1
    assert response.json()['artifacts_requested']==['resume']

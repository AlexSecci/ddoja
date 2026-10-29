import json

import pytest

from app.store import Store,Conflict
from app.discovery_inventory import accounting,native_text,saved_board_rows,upsert
from test_efficiency_controls import start


def fixture(tmp_path, *, older=False):
    s=Store(tmp_path);p=s.create_agent_pipeline('Fictional inventory','https://job-boards.greenhouse.io/fixture')
    worker=s.list_agent_workers(p['id'])[0];launch=start(s,worker)
    result=s.agent_action(worker['id'],launch['capability'],'record_listing',{'url':'https://job-boards.greenhouse.io/fixture/jobs/101',
        'requisition_id':'101','title':'Software Engineer','jd':'Complete synthetic description with C++ responsibilities.',
        'selected':True,'metadata':{'location':'Austin, TX','country':'US'}})
    if older:
        s.agent_action(worker['id'],launch['capability'],'record_listing',{'url':'https://job-boards.greenhouse.io/fixture/jobs/303',
            'requisition_id':'303','title':'Older Software Engineer','jd':'Complete older synthetic job description.',
            'selected':True,'metadata':{'location':'Austin, TX','country':'US'}})
    return s,p,worker,launch,result


def board(s,worker):
    text='''### Page
- Page URL: https://job-boards.greenhouse.io/fixture?page=1
### Snapshot
  - heading "2 jobs" [level=2]
  - row:
    - link "Software Engineer Austin, TX" [ref=e1]:
      - /url: https://job-boards.greenhouse.io/fixture/jobs/101
      - paragraph [ref=e2]: Software Engineer
      - paragraph [ref=e3]: Austin, TX
  - row:
    - link "Software Engineer London, UK" [ref=e4]:
      - /url: https://job-boards.greenhouse.io/fixture/jobs/202
      - paragraph [ref=e5]: Software Engineer
      - paragraph [ref=e6]: London, UK
  - link "Privacy" [ref=e7]:
    - /url: https://example.invalid/privacy
'''
    return s.agent_runtime_event(worker['id'],{'type':'tool_result','tool_name':'playwright.browser_snapshot',
        'success':True,'session_id':s.get_agent_worker(worker['id'])['session_id'],
        'transport_event':{'type':'item.completed','item':{'type':'mcp_tool_call','server':'playwright',
            'tool':'browser_snapshot','arguments':{},'status':'completed','result':{'content':[{'type':'text','text':text}]}}}})


def test_all_observed_identities_count_without_creating_jds_or_evaluators(tmp_path):
    s,p,w,launch,_=fixture(tmp_path);before=len(s.list_agent_workers(p['id']));board(s,w)
    c=s.agent_pipeline_counts(p['id'])
    assert c['discovered']==2 and c['selected']==1 and c['full_jd_records']==1
    assert c['excluded']==0 and c['unreviewed_listings']==1
    s.agent_action(w['id'],launch['capability'],'record_discovery_inventory',{'listings':[{
        'url':'https://job-boards.greenhouse.io/fixture/jobs/202','disposition':'excluded',
        'reason':'The observed location is London, UK; this campaign requires the United States.'}]})
    c=s.agent_pipeline_counts(p['id'])
    assert c['discovered']==2 and c['excluded']==1 and c['unreviewed_listings']==0
    assert len(s.list_agent_workers(p['id']))==before
    with s.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]==1
        assert db.execute('SELECT COUNT(*) FROM job_versions').fetchone()[0]==1
    result=s.agent_action(w['id'],launch['capability'],'finish_discovery',{
        'status':'complete','detail':'Both observed roles accounted for.','coverage':{'listing_total':2}})
    assert result['status']=='complete'


@pytest.mark.parametrize('case',['unreviewed','missing','enumerated'])
def test_complete_is_rejected_when_reported_listings_are_not_accounted_for(tmp_path,case):
    s,p,w,launch,_=fixture(tmp_path)
    if case=='unreviewed':board(s,w)
    with pytest.raises(Conflict,match='accounting is unresolved'):
        s.agent_action(w['id'],launch['capability'],'finish_discovery',{
            'status':'complete','detail':'Must not be trusted.',
            'coverage':{'listing_total':None,'enumerated_total':2} if case=='enumerated' else {'listing_total':2}})
    assert s.get_agent_worker(w['id'])['state']=='running'


@pytest.mark.parametrize('entry',[
    {'url':'https://job-boards.greenhouse.io/fixture/jobs/202','disposition':'excluded'},
    {'url':'https://job-boards.greenhouse.io/fixture/jobs/999','title':'Invented','disposition':'excluded','reason':'Unobserved'},
    {'url':'https://job-boards.greenhouse.io/fixture/jobs/202','disposition':'selected'},
])
def test_inventory_requires_native_identity_and_a_real_exclusion_reason(tmp_path,entry):
    s,p,w,launch,_=fixture(tmp_path);board(s,w)
    with pytest.raises((ValueError,Conflict)):
        s.agent_action(w['id'],launch['capability'],'record_discovery_inventory',{'listings':[entry]})
    assert s.agent_pipeline_counts(p['id'])['unreviewed_listings']==1


@pytest.mark.parametrize('reported_total',[2,5])
def test_historical_recovery_preserves_reported_provenance_and_does_not_start_work(tmp_path,reported_total):
    s,p,w,_,_=fixture(tmp_path,older=True);board(s,w)
    with s.connect(write=True) as db:
        # Emulate the old database: native board evidence exists, but no index.
        db.execute('DELETE FROM agent_discovery_inventory')
        db.execute("UPDATE agent_workers SET state='completed',turn_active=0 WHERE id=?",(w['id'],))
        db.execute("UPDATE pipelines SET discovery_status='complete',discovery_payload=? WHERE id=?",(
            json.dumps({'status':'complete','worker_id':w['id'],'coverage':{'listing_total':reported_total,'remaining_paths':[]}}),p['id']))
        workers=[tuple(r) for r in db.execute('SELECT id,state,turn_active FROM agent_workers ORDER BY id')]
        runs=db.execute('SELECT COUNT(*) FROM agent_execution_runs').fetchone()[0]
    recovered=s.recover_discovery_inventory(p['id'])
    assert recovered['listing_identities']==3 and recovered['full_jd_records']==2
    assert recovered['historical_exclusions']==(1 if reported_total==2 else 0)
    assert recovered['unreviewed_listings']==(0 if reported_total==2 else 1)
    s.recover_discovery_inventory(p['id'])
    inventory=s.discovery_inventory(p['id'])
    assert inventory['total']==3
    excluded=[r for r in inventory['rows'] if r['disposition']=='excluded']
    if excluded:
        assert excluded[0]['decision_source']=='historical_report'
        assert 'individual filter reason was not saved' in excluded[0]['reason']
        assert inventory['accounting']['coverage_accounted'] is True
        assert inventory['accounting']['coverage_verified'] is False
    with s.connect() as db:
        assert workers==[tuple(r) for r in db.execute('SELECT id,state,turn_active FROM agent_workers ORDER BY id')]
        assert runs==db.execute('SELECT COUNT(*) FROM agent_execution_runs').fetchone()[0]
        assert db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]==2


def test_revisiting_a_page_does_not_erase_same_pass_exclusion_decisions(tmp_path):
    s,p,w,launch,_=fixture(tmp_path);board(s,w)
    s.agent_action(w['id'],launch['capability'],'record_discovery_inventory',{'listings':[{
        'url':'https://job-boards.greenhouse.io/fixture/jobs/202','disposition':'excluded','reason':'London, UK is outside the saved US geography filter.'}]})
    board(s,w)
    assert s.agent_pipeline_counts(p['id'])['excluded']==1
    assert s.agent_pipeline_counts(p['id'])['unreviewed_listings']==0


def test_inventory_batch_rolls_back_if_one_identity_is_unobserved(tmp_path):
    s,p,w,launch,_=fixture(tmp_path);board(s,w)
    with pytest.raises(Conflict,match='absent'):
        s.agent_action(w['id'],launch['capability'],'record_discovery_inventory',{'listings':[
            {'url':'https://job-boards.greenhouse.io/fixture/jobs/202','disposition':'excluded','reason':'Outside the saved US filter.'},
            {'url':'https://job-boards.greenhouse.io/fixture/jobs/999','title':'Absent','disposition':'excluded','reason':'Unobserved'},
        ]})
    assert s.agent_pipeline_counts(p['id'])['unreviewed_listings']==1
    assert s.agent_pipeline_counts(p['id'])['excluded']==0


def test_current_pass_does_not_need_to_reclassify_old_listings_absent_from_board(tmp_path):
    s,p,w,launch,_=fixture(tmp_path);board(s,w)
    with s.connect(write=True) as db:
        db.execute("UPDATE agent_discovery_inventory SET worker_id=NULL,disposition='excluded',decision_source='historical_report' WHERE requisition_id='202'")
        audit=accounting(db,p['id'],{'listing_total':1},w['id'])
        assert audit['historical_exclusions']==1 and audit['pass_historical_exclusions']==0
        assert audit['coverage_verified'] is True
    result=s.agent_action(w['id'],launch['capability'],'finish_discovery',{
        'status':'complete','detail':'Only current opening accounted for.','coverage':{'listing_total':1}})
    assert result['status']=='complete'


def test_inventory_reads_are_scoped_and_paged(tmp_path):
    from test_ui_contract import _client
    s,p,w,launch,_=fixture(tmp_path);board(s,w)
    other=s.create_agent_pipeline('Other fixture','https://job-boards.greenhouse.io/other')
    with s.connect(write=True) as db:
        upsert(db,other['id'],{'url':'https://job-boards.greenhouse.io/other/jobs/808','title':'Other campaign role'},observed_at='fixture',now='fixture')
    result=s.agent_action(w['id'],launch['capability'],'get_discovery_inventory',{'disposition':'all','limit':1})
    assert result['total']==2 and len(result['rows'])==1
    assert all(r['pipeline_id']==p['id'] for r in result['rows'])
    _,client,headers,_=_client(tmp_path)
    result=client.get('/api/pipelines/'+p['id']+'/discovery-inventory?disposition=unreviewed&limit=1',headers=headers)
    assert result.status_code==200,result.text
    assert result.json()['total']==1 and result.json()['rows'][0]['requisition_id']=='202'
    assert client.get('/api/pipelines/'+p['id']+'/discovery-inventory?limit=101',headers=headers).status_code==422


@pytest.mark.parametrize('payload',[{}, {'transport_event':None}, {'transport_event':{'item':{'result':[]}}},
    {'transport_event':{'item':{'result':{'content':[{'type':'image','text':None}]}}}}])
def test_nontext_runtime_events_do_not_break_identity_capture(payload):
    assert native_text(payload)==''


def test_flattened_native_links_preserve_the_exact_accessible_label():
    rows=saved_board_rows('''### Page
- Page URL: https://job-boards.greenhouse.io/fixture?page=2
  - row:
    - link "Facilities Engineer - Memphis Southaven, MS; Memphis, TN" [ref=f4e36] [cursor=pointer]:
      - /url: https://job-boards.greenhouse.io/fixture/jobs/202
  - row:
    - link "Software Engineer Austin, TX" [ref=f4e37] [cursor=pointer]:
      - /url: https://job-boards.greenhouse.io/fixture/jobs/303
''')
    assert len(rows)==2
    assert rows[0]['title']=='Facilities Engineer - Memphis Southaven, MS; Memphis, TN'
    assert rows[0]['location']==''  # No invented split of the label into fields.
    assert rows[1]['requisition_id']=='303'


def test_backlog_view_keeps_scored_unscored_and_legacy_but_hides_all_submitted(tmp_path):
    from test_efficiency_controls import seed
    from test_ui_contract import _client
    s,p,w,_,entries=seed(tmp_path,6);ids=[x[0]['job_id'] for x in entries]
    with s.connect(write=True) as db:
        db.execute('UPDATE pipeline_jobs SET fit_score=75,fit_jd_version=1,fit_profile_version=? WHERE job_id=?',(s.profile_version(db),ids[0]))
        db.execute("UPDATE jobs SET phase='submitted' WHERE id=?",(ids[3],))
        db.execute("UPDATE pipeline_jobs SET agent_status='submitted' WHERE job_id=?",(ids[4],))
        parent=s._agent_insert_worker(db,p['id'],'submission',job_id=ids[5])
        db.execute("INSERT INTO agent_submission_results(id,pipeline_id,worker_id,job_id,state,detail,observations,event_ids,created_at) "
            "VALUES('synthetic-receipt',?,?,?,'submitted','Synthetic verified receipt','{}','[]','fixture')",(p['id'],parent,ids[5]))
    app,client,headers,_=_client(tmp_path)
    response=client.get('/api/pipelines/'+p['id']+'/jobs',headers=headers)
    assert response.status_code==200,response.text
    full=response.json()['jobs']
    visible=client.get('/api/pipelines/'+p['id']+'/jobs?view=backlog',headers=headers).json()['jobs']
    assert len(full)==6 and {r['id'] for r in visible}==set(ids[:3])
    assert next(r for r in full if r['id']==ids[5])['has_submission_receipt'] is True

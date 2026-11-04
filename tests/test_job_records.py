import json
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.server import create_app
from app.store import Conflict, Store
from test_efficiency_controls import seed, start
from test_ranked_evaluation import scored
from test_ui_contract import IsolatedAgentManager


def evaluate_all(store, pipeline, scores):
    workers = [w for w in store.list_agent_workers(pipeline['id']) if w['role'] == 'evaluator']
    for w, score in zip(workers, scores):
        launch = start(store, w)
        store.agent_action(w['id'], launch['capability'], 'finish_evaluation', scored(score))
        store.finish_agent_turn(w['id'], {'disposition': 'completed'})
    return workers


@pytest.mark.parametrize('status', ['submitted', 'discarded'])
def test_manual_terminal_status_survives_restart_resume_rediscovery_and_blocks_existing_queue(tmp_path, status):
    store, pipeline, _, _, listings = seed(tmp_path, 2)
    workers = evaluate_all(store, pipeline, [95, 70])
    queued = store.prepare_ranked_submissions()
    target = next(store.get_agent_worker(wid) for wid in queued if store.get_agent_worker(wid)['job_id'] == workers[0]['job_id'])
    result = store.set_manual_job_status(target['job_id'], status)
    assert result['manual']
    store = Store(tmp_path)
    assert store.get_job(target['job_id'])['manual_status'] == status
    assert store.get_agent_worker(target['id'])['state'] == 'stopped'
    store.resume_agent_pipeline(pipeline['id'], manual_start=True, stage='submission')
    assert not store.reserve_agent_worker(target['id'])
    assert target['id'] not in store.list_queued_agent_workers()
    # A changed description must not clear this human decision.
    original = next(payload for record, payload in listings if record['job_id'] == target['job_id'])
    payload = {**original, **original['metadata'], 'jd': 'Updated full description. Additional requirement.'}
    payload['company_id'] = pipeline['company_id']
    store.discover(pipeline['company_id'], payload)
    assert store.get_job(target['job_id'])['phase'] == status
    assert store.prepare_ranked_submissions() == []
    row = next(r for r in store.job_records() if r['id'] == target['job_id'])
    assert row['record_status'] == status
    assert not row['has_submission_receipt']
    counts = store.agent_pipeline_counts(pipeline['id'])
    assert counts[status] == 1
    assert counts['scored_backlog'] == 1
    with store.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM agent_submission_results').fetchone()[0] == 0


def test_manual_decision_keeps_uncertain_history(tmp_path):
    store, pipeline, _, _, listings = seed(tmp_path, 1)
    job_id = listings[0][0]['job_id']
    with store.connect(write=True) as db:
        uncertain = store._agent_insert_worker(db, pipeline['id'], 'submission', job_id=job_id,
            context={'final_action_may_have_been_sent': True})
        db.execute("UPDATE agent_workers SET state='uncertain',session_id='retained-history' WHERE id=?", (uncertain,))
        store._agent_set_browser_control(db, 'company:' + pipeline['company_id'], 'company_auth', 'open',
            'Current email verification', 'Synthetic fixture', uncertain, [])
    before = store.get_agent_worker(uncertain)
    store.set_manual_job_status(job_id, 'submitted', 'Applied externally; confirmed by applicant.')
    assert store.get_agent_worker(uncertain) == before
    with store.connect() as db:
        assert db.execute("SELECT state FROM agent_browser_controls WHERE worker_id=?", (uncertain,)).fetchone()[0] == 'closed'
    assert store.job_records(state='submitted')[0]['manual_status_note'].startswith('Applied externally')
    store.set_manual_job_status(job_id, 'submitted')
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM events WHERE kind='job.manual_status'").fetchone()[0] == 1


@pytest.mark.parametrize('status', ['submitted', 'discarded'])
def test_fresh_campaign_cannot_apply_to_a_manually_closed_job(tmp_path, status):
    store, first, discovery, _, listings = seed(tmp_path, 1)
    job_id = listings[0][0]['job_id']
    store.set_manual_job_status(job_id, status)
    store.request_agent_pipeline_stop(first['id'])
    store.finish_agent_turn(discovery['id'], {'disposition': 'stopped'})
    second = store.create_agent_pipeline('Synthetic Efficiency', 'https://fictional.invalid/careers')
    store.add_pipeline_job(second['id'], job_id)
    with store.connect(write=True) as db:
        wid = store._agent_insert_worker(db, second['id'], 'submission', job_id=job_id)
    assert not store.reserve_agent_worker(wid)
    assert wid not in store.list_queued_agent_workers()
    assert store.agent_pipeline_counts(second['id'])[status] == 1
    rows = store.job_records(state=status)
    assert len(rows) == 1 and rows[0]['id'] == job_id
def test_manual_status_refuses_active_worker_and_cannot_discard_a_submitted_job(tmp_path):
    store, pipeline, _, _, listings = seed(tmp_path, 1)
    worker = next(w for w in store.list_agent_workers(pipeline['id']) if w['role'] == 'evaluator')
    launch = start(store, worker)
    with pytest.raises(Conflict, match='worker is active'):
        store.set_manual_job_status(worker['job_id'], 'submitted')
    assert store.get_job(worker['job_id'])['manual_status'] == ''
    store.finish_agent_turn(worker['id'], {'disposition': 'waiting'})
    store.set_manual_job_status(worker['job_id'], 'submitted')
    with pytest.raises(Conflict, match='already submitted'):
        store.set_manual_job_status(worker['job_id'], 'discarded')


def test_catalog_filters_company_status_and_orders_company_then_score_without_duplicates(tmp_path):
    store, pipeline, _, _, listings = seed(tmp_path, 3)
    evaluate_all(store, pipeline, [20, 95, 65])
    store.company('alpha', 'Alpha Company', 'https://fixture.invalid')
    external = store.discover('alpha', {'url': 'https://fixture.invalid/other', 'title': 'Unscored job',
        'jd': 'Full description.', 'requisition_id': 'external', 'country': 'US',
        'employment': 'full_time', 'arrangement': 'hybrid', 'location': 'Austin'})
    assert [r['fit_score'] for r in store.job_records(order='score_desc')] == [95, 65, 20, None]
    assert [r['fit_score'] for r in store.job_records(order='score_asc')] == [20, 65, 95, None]
    assert store.job_records(order='company')[0]['id'] == external['id']
    assert [r['fit_score'] for r in store.job_records(order='company')][1:] == [95, 65, 20]
    assert len(store.job_records(state='scored', company_id=pipeline['company_id'])) == 3
    assert [r['id'] for r in store.job_records(state='unscored')] == [external['id']]
    assert store.job_records(query='EXTERNAL')[0]['id'] == external['id']
    with store.connect(write=True) as db:
        db.execute("UPDATE meta SET value=CAST(value AS INTEGER)+1 WHERE key='profile_version'")
    assert not store.job_records(state='scored')
    assert len(store.job_records(state='needs_rescoring')) == 3
    assert all(row['fit_score'] is None for row in store.job_records())


def test_job_record_routes_require_session_token_and_do_not_start_campaigns(tmp_path):
    store, pipeline, _, _, listings = seed(tmp_path, 1)
    app = create_app(tmp_path, agent_manager_factory=IsolatedAgentManager)
    with TestClient(app) as client:
        endpoint = '/api/jobs/' + listings[0][0]['job_id'] + '/manual-status'
        assert client.post(endpoint, json={'status': 'submitted'}).status_code == 403
        headers = {'X-Pipeline-Token': client.get('/api/session').json()['token']}
        assert client.post(endpoint, headers=headers, json={'status': 'submitted'}).status_code == 200
        payload = client.get('/api/jobs?state=submitted&order=company', headers=headers).json()
        assert len(payload) == 1 and payload[0]['manual_status'] == 'submitted'
        assert client.get('/api/jobs?order=invalid', headers=headers).status_code == 422
        assert app.state.agent_pipeline_manager.schedule_calls == 1


def test_catalog_ui_filters_preserve_zero_scores_and_company_secondary_order():
    path = Path(__file__).resolve().parents[1] / 'app/static/app.js'
    script = r'''
const fs = require('fs'), vm = require('vm'), assert = require('assert/strict');
const source = fs.readFileSync(process.argv[1], 'utf8');
const start = source.indexOf('function filterJobRecords('), end = source.indexOf('function manualJobButtons(', start);
const ctx = {}; vm.runInNewContext(source.slice(start,end)+';this.filter=filterJobRecords;',ctx);
const rows = [{id:'1',title:'B',company_id:'b',company_name:'Beta',fit_score:0,fit_current:true,record_status:'discarded'},
{id:'2',title:'A',company_id:'a',company_name:'Alpha',fit_score:40,fit_current:true,record_status:'scored'},
{id:'3',title:'C',company_id:'a',company_name:'Alpha',fit_score:90,fit_current:true,record_status:'submitted'},
{id:'4',title:'D',company_id:'a',company_name:'Alpha',fit_score:null,fit_current:false,record_status:'unscored'}];
assert.deepEqual(Array.from(ctx.filter(rows,{order:'company'}),r=>r.id),['3','2','4','1']);
assert.deepEqual(Array.from(ctx.filter(rows,{order:'score_asc'}),r=>r.id),['1','2','3','4']);
assert.deepEqual(Array.from(ctx.filter(rows,{state:'scored'}),r=>r.id),['2']);
assert.deepEqual(Array.from(ctx.filter(rows,{state:'unscored'}),r=>r.id),['4']);
assert.deepEqual(Array.from(ctx.filter(rows,{state:'discarded'}),r=>r.id),['1']);
assert.equal(ctx.filter(rows,{company:'a',state:'submitted'}).length,1);
assert.equal(ctx.filter(rows,{query:'Beta'})[0].id,'1');
'''
    subprocess.run(['node', '-e', script, str(path)], check=True, capture_output=True, text=True)


def test_every_catalog_state_filter_matches_the_displayed_status(tmp_path):
    from app.job_records import STATES

    states = sorted(STATES - {'all'})
    store, pipeline, _, _, _ = seed(tmp_path, len(states))
    workers = evaluate_all(store, pipeline, [70] * len(states))
    by_state = dict(zip(states, workers))
    # Keep scores on terminal records to reproduce the misleading Scored filter.
    for state in ('submitted', 'discarded'):
        store.set_manual_job_status(by_state[state]['job_id'], state)
    with store.connect(write=True) as db:
        for state in ('unscored', 'legacy', 'failed'):
            db.execute("UPDATE pipeline_jobs SET fit_score=NULL,fit_jd_version=NULL,"
                       "fit_profile_version=NULL,agent_decision='',agent_status='selected' WHERE job_id=?",
                       (by_state[state]['job_id'],))
        db.execute("UPDATE pipeline_jobs SET agent_decision='approve' WHERE job_id=?",
                   (by_state['legacy']['job_id'],))
        db.execute("UPDATE pipeline_jobs SET fit_profile_version=-1 WHERE job_id=?",
                   (by_state['needs_rescoring']['job_id'],))
        db.execute("UPDATE jobs SET run_status='submission_uncertain' WHERE id=?",
                   (by_state['uncertain']['job_id'],))
        db.execute("UPDATE pipeline_jobs SET selected=0 WHERE job_id=?", (by_state['excluded']['job_id'],))
        db.execute("UPDATE agent_workers SET turn_active=1 WHERE id=?", (by_state['active']['id'],))
        db.execute("UPDATE agent_workers SET state='failed' WHERE id=?", (by_state['failed']['id'],))

    app = create_app(tmp_path, agent_manager_factory=IsolatedAgentManager)
    with TestClient(app) as client:
        headers = {'X-Pipeline-Token': client.get('/api/session').json()['token']}
        all_rows = client.get('/api/jobs', headers=headers).json()
        assert len(all_rows) == len(states)
        assert {row['record_status'] for row in all_rows} == set(states)
        for state, worker in by_state.items():
            rows = client.get('/api/jobs', params={'state': state}, headers=headers).json()
            assert [row['id'] for row in rows] == [worker['job_id']], state
            assert rows[0]['record_status'] == state

import json
import time

import pytest

from app.pipelines import AgentPipelineManager
from app.store import Conflict
from test_efficiency_controls import seed


def prepared(tmp_path):
    store, pipeline, discovery, launch, listings = seed(tmp_path, count=2)
    store.finish_agent_turn(discovery["id"], {"disposition": "interrupted", "checkpoint": "fixture saved"})
    settings = store.get_worker_settings()
    settings["roles"] = {role: {"provider": "deepseek", "model": "deepseek-flash", "effort": "low", "browser_backend": "none" if role == "materials" else "playwright"} for role in settings["roles"]}
    store.save_worker_settings(settings["revision"], settings["roles"])
    jobs = [entry[0]["job_id"] for entry in listings]
    return store, pipeline, discovery, jobs


def expire(store, pipeline_id):
    with store.connect(write=True) as db:
        key = 'agent_trial:' + pipeline_id
        policy = json.loads(db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()[0])
        policy['deadline_unix'] = time.time() - 1
        db.execute('UPDATE meta SET value=? WHERE key=?', (json.dumps(policy), key))


def test_trial_admits_only_selected_jobs_and_deepseek_then_pins_switched_session(tmp_path):
    store, pipeline, discovery, jobs = prepared(tmp_path)
    store.configure_agent_trial(pipeline['id'], jobs[:1])
    store.resume_agent_pipeline(pipeline['id'])
    evaluators = [w for w in store.list_agent_workers(pipeline['id']) if w['role'] == 'evaluator']
    excluded = next(w for w in evaluators if w['job_id'] == jobs[1])
    assert not store.reserve_agent_worker(excluded['id'])
    settings = store.get_worker_settings(); original = json.loads(json.dumps(settings['roles']))
    settings['roles']['evaluation'].update(provider='openai', model='gpt-6.1-sol', effort='high', browser_backend='cua')
    store.save_worker_settings(settings['revision'], settings['roles'])
    allowed = next(w for w in evaluators if w['job_id'] == jobs[0])
    assert not store.reserve_agent_worker(allowed['id'])
    store.save_worker_settings(store.get_worker_settings()['revision'], original)
    assert store.reserve_agent_worker(discovery['id'])
    migrated = store.get_agent_worker(discovery['id'])
    assert migrated['context']['execution']['provider'] == 'deepseek'
    assert migrated['session_id'] is None
    assert migrated['context']['execution_history'][-1]['session_id']


def test_trial_limits_discovery_turns_and_total_invocations(tmp_path):
    store, pipeline, discovery, jobs = prepared(tmp_path)
    store.configure_agent_trial(pipeline['id'], jobs[:1], max_invocations=2)
    store.resume_agent_pipeline(pipeline['id'])
    assert store.reserve_agent_worker(discovery['id'])
    store.agent_launch_context(discovery['id'])
    store.finish_agent_turn(discovery['id'], {'disposition': 'interrupted'})
    with store.connect(write=True) as db:
        db.execute("UPDATE agent_workers SET state='queued' WHERE id=?", (discovery['id'],))
    assert not store.reserve_agent_worker(discovery['id'])
    evaluator = next(w for w in store.list_agent_workers(pipeline['id']) if w['role']=='evaluator' and w['job_id']==jobs[0])
    assert store.reserve_agent_worker(evaluator['id'])
    store.agent_launch_context(evaluator['id'])
    store.finish_agent_turn(evaluator['id'], {'disposition': 'interrupted'})
    with store.connect(write=True) as db:
        db.execute("UPDATE agent_workers SET state='queued' WHERE id=?", (evaluator['id'],))
    assert not store.reserve_agent_worker(evaluator['id'])
    assert 'invocation limit' in store.agent_trial_stop_reason(pipeline['id'])


def test_expired_trial_rejects_resumes_and_final_actions_and_watchdog_stops(tmp_path):
    store, pipeline, discovery, jobs = prepared(tmp_path)
    store.configure_agent_trial(pipeline['id'], jobs[:1])
    expire(store, pipeline['id'])
    with pytest.raises(Conflict, match='bounded trial ended'):
        store.resume_agent_pipeline(pipeline['id'])
    with store.connect(write=True) as db:
        worker = dict(db.execute('SELECT * FROM agent_workers WHERE role=? AND job_id=?', ('evaluator', jobs[0])).fetchone())
        with pytest.raises(Conflict, match='bounded trial'):
            store._agent_begin_final_action(db, worker, {})
    manager = AgentPipelineManager(store, runtime_factory=lambda *_: pytest.fail('Expired trial must not launch a runtime'))
    try:
        manager.schedule()
        deadline = time.monotonic()+4
        while time.monotonic() < deadline and store.get_agent_trial(pipeline['id'])['state'] == 'active':
            time.sleep(.05)
        assert store.get_agent_trial(pipeline['id'])['state'] == 'ended'
        assert store.get_pipeline(pipeline['id'])['stop_requested']
    finally:
        manager.shutdown()


def test_preparing_trial_preserves_quota_and_rejects_possible_send(tmp_path):
    store, pipeline, _, jobs = prepared(tmp_path)
    store.set_agent_quota(True, reason='Fixture quota hold')
    store.configure_agent_trial(pipeline['id'], jobs[:1])
    assert store.get_agent_quota()['paused']
    with store.connect(write=True) as db:
        submission = store._agent_insert_worker(db, pipeline['id'], 'submission', job_id=jobs[0])
        db.execute("UPDATE agent_workers SET context=? WHERE id=?", (json.dumps({'final_action_may_have_been_sent': True}), submission))
    with pytest.raises(Conflict, match='possibly sent'):
        store.configure_agent_trial(pipeline['id'], jobs[:1])

import pytest

from app.store import Conflict
from test_efficiency_controls import seed, start
from test_ranked_evaluation import scored


def evaluator(store, pipeline):
    return next(w for w in store.list_agent_workers(pipeline['id']) if w['role'] == 'evaluator')


def change_profile(store, tmp_path):
    source = tmp_path / 'updated-applicant.md'
    source.write_text('New confirmed applicant evidence.\n', encoding='utf-8')
    store.import_document('application_profile', source)
    return store.profile_version()


def test_resumed_evaluator_receives_current_snapshot_and_full_evidence_in_same_session(tmp_path):
    store, pipeline, _, _, _ = seed(tmp_path, 1)
    worker = evaluator(store, pipeline)
    first = start(store, worker)
    old = store.get_agent_worker(worker['id'])
    contract = first['task']['task_context']['instruction_contract_id']
    store.agent_action(worker['id'], first['capability'], 'checkpoint', {
        'checkpoint': 'assessment_waiting_for_host', 'context': {'assessment': {'score': 80}}})
    store.finish_agent_turn(worker['id'], {'disposition': 'waiting'})
    current_profile = change_profile(store, tmp_path)
    store.resume_agent_pipeline(pipeline['id'], manual_start=True, stage='evaluation')
    launch = start(store, store.get_agent_worker(worker['id']))
    task = launch['task']; refreshed = store.get_agent_worker(worker['id'])
    assert task['worker']['profile_version'] == current_profile == refreshed['profile_version']
    assert task['task_context']['applicant_index']['profile_version'] == current_profile
    assert task['task_context']['job']['jd_version'] == refreshed['jd_version']
    assert task['task_context']['evaluation_snapshot_refresh']['previous_snapshot']['profile_version'] == old['profile_version']
    assert task['task_context']['instruction_contract_id'] == contract
    assert refreshed['session_id'] == old['session_id']
    assert refreshed['context']['assessment'] == {'score': 80}
    result = store.agent_action(worker['id'], launch['capability'], 'finish_evaluation', scored(65))
    assert result['score'] == 65 and result['profile_version'] == current_profile
    assert result['submission_worker_id'] is None


@pytest.mark.parametrize('change', ['profile', 'jd'])
def test_midturn_change_requires_joint_fresh_read_before_score_can_commit(tmp_path, change):
    store, pipeline, _, _, _ = seed(tmp_path, 1)
    worker = evaluator(store, pipeline); launch = start(store, worker)
    if change == 'profile':
        change_profile(store, tmp_path)
    else:
        store.agent_action(worker['id'], launch['capability'], 'update_job_description', {
            'jd': 'Changed JD with materially different confirmed requirements.'})
        # Updating the JD through the worker already binds that worker to the
        # new JD; emulate an independent discovery update for this stale read.
        with store.connect(write=True) as db:
            db.execute('UPDATE agent_workers SET jd_version=? WHERE id=?', (worker['jd_version'], worker['id']))
    for sections in [[], ['job'], ['applicant_index']]:
        store.agent_action(worker['id'], launch['capability'], 'get_context', {'sections': sections})
        with pytest.raises(Conflict, match='snapshot changed'):
            store.agent_action(worker['id'], launch['capability'], 'finish_evaluation', scored(90))
    packet = store.agent_action(worker['id'], launch['capability'], 'get_context', {
        'sections': ['job', 'applicant_index']})
    assert 'evaluation_snapshot_refresh' in packet
    saved = store.get_agent_worker(worker['id'])
    assert saved['jd_version'] == packet['sections']['job']['jd_version']
    assert saved['profile_version'] == packet['sections']['applicant_index']['profile_version']
    result = store.agent_action(worker['id'], launch['capability'], 'finish_evaluation', scored(40))
    assert result['score'] == 40 and result['profile_version'] == saved['profile_version']


@pytest.mark.parametrize('role', ['submission', 'document'])
def test_joint_context_read_never_rebinds_application_or_document_snapshot(tmp_path, role):
    store, pipeline, _, _, listings = seed(tmp_path, 1)
    with store.connect(write=True) as db:
        wid = store._agent_insert_worker(db, pipeline['id'], role, job_id=listings[0][0]['job_id'])
        db.execute("UPDATE agent_workers SET state='running',turn_active=1 WHERE id=?", (wid,))
    launch = store.agent_launch_context(wid)
    before = store.get_agent_worker(wid)
    change_profile(store, tmp_path)
    packet = store.agent_action(wid, launch['capability'], 'get_context', {
        'sections': ['job', 'applicant_index']})
    assert 'evaluation_snapshot_refresh' not in packet
    assert store.get_agent_worker(wid)['profile_version'] == before['profile_version']


def test_stopped_pipeline_context_read_cannot_rebind_even_an_evaluator(tmp_path):
    store, pipeline, _, _, _ = seed(tmp_path, 1)
    worker = evaluator(store, pipeline); launch = start(store, worker)
    before = store.get_agent_worker(worker['id'])
    change_profile(store, tmp_path)
    store.request_agent_pipeline_stop(pipeline['id'])
    packet = store.agent_action(worker['id'], launch['capability'], 'get_context', {
        'sections': ['job', 'applicant_index']})
    assert 'evaluation_snapshot_refresh' not in packet
    assert store.get_agent_worker(worker['id'])['profile_version'] == before['profile_version']

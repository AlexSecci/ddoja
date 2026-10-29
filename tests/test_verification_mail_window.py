import json
from datetime import datetime, timezone

import pytest

from app.auth_broker import verification_mail_window
from app.store import Store


def setup_window(tmp_path, monkeypatch):
    store = Store(tmp_path)
    pipeline = store.create_agent_pipeline('Fictional', 'https://job-boards.greenhouse.io/fictional')
    found = store.discover(pipeline['company_id'], {'url': 'https://job-boards.greenhouse.io/fictional/jobs/123',
        'requisition_id': '123', 'title': 'Software Engineer', 'jd': 'Synthetic full description.',
        'country': 'US', 'employment': 'full_time', 'arrangement': 'hybrid', 'location': 'Austin'})
    job = store.get_job(found['id'])
    store.add_pipeline_job(pipeline['id'], job['id'])
    with store.connect(write=True) as db:
        wid = store._agent_insert_worker(db, pipeline['id'], 'submission', job_id=job['id'])
    worker = store.get_agent_worker(wid)
    monkeypatch.setattr('app.auth_broker.time.time', lambda: 1000)

    def event(when, tool='browser_snapshot', label=None, url=None, proof=None):
        value = {'observed_application_url': url or job['url'], 'transport_event': {'item': {
            'arguments': {'element': label} if label else {}}}}
        if proof is not None:
            value['verification_observation'] = proof
        with store.connect(write=True) as db:
            db.execute('INSERT INTO agent_runtime_events(id,worker_id,event_type,tool_name,success,payload,created_at) '
                'VALUES(?,?,?,?,?,?,?)', (str(when), wid, 'tool_result', 'playwright.'+tool, 1,
                json.dumps(value), datetime.fromtimestamp(when, timezone.utc).isoformat()))
    return store, worker, job, event


def test_delayed_checkpoint_uses_exact_observed_challenge_window(tmp_path, monkeypatch):
    s, w, j, event = setup_window(tmp_path, monkeypatch)
    event(600, 'browser_click', 'Submit application button')
    event(602, proof={'present': True, 'filled_boxes': 0, 'submit_enabled': False})
    event(900, proof={'present': True, 'filled_boxes': 0, 'submit_enabled': False})
    assert verification_mail_window(s, w, j) == (598, 605)


def test_redacted_submit_is_bound_to_current_host_final_action(tmp_path, monkeypatch):
    s,w,j,event=setup_window(tmp_path,monkeypatch)
    w['context']={'final_action_authorized_at':datetime.fromtimestamp(700,timezone.utc).isoformat()}
    event(600,'browser_click','Submit application button')
    event(602,proof={'present':True,'filled_boxes':0,'submit_enabled':False})
    event(702,'browser_click',proof={'present':True,'filled_boxes':0,'submit_enabled':False})
    assert verification_mail_window(s,w,j)==(700,705)


@pytest.mark.parametrize('case', ['different_page', 'no_challenge', 'resend', 'second_submit', 'too_old', 'slow_transition'])
def test_unproved_or_superseded_window_is_not_reused(tmp_path, monkeypatch, case):
    s, w, j, event = setup_window(tmp_path, monkeypatch)
    event(600, 'browser_click', 'Submit application button', url='https://unrelated.example/' if case == 'different_page' else None)
    if case != 'no_challenge':
        event(630 if case == 'slow_transition' else 602,
              proof={'present': True, 'filled_boxes': 0, 'submit_enabled': False})
    if case == 'resend': event(700, 'browser_click', 'Resend code')
    if case == 'second_submit': event(700, 'browser_click', 'Submit application button')
    if case == 'too_old': monkeypatch.setattr('app.auth_broker.time.time', lambda: 2000)
    assert verification_mail_window(s, w, j) is None

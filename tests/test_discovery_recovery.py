import pytest

from app.store import Conflict
from test_efficiency_controls import seed, start
from test_ranked_evaluation import scored


@pytest.mark.parametrize('href,url,allowed', [
    ('/jobs/202', 'https://fictional.invalid/jobs/202', True),
    ('jobs/results/202', 'https://fictional.invalid/about/careers/applications/jobs/results/202', True),
    ('jobs/results/202', 'https://fictional.invalid/invented/jobs/results/202', False),
    ('jobs/results/202', 'https://other.invalid/about/careers/applications/jobs/results/202', False),
])
def test_native_relative_inventory_urls_resolve_only_observed_paths(tmp_path, href, url, allowed):
    store, pipeline, worker, launch, _ = seed(tmp_path, 0)
    text = ('### Page\n- Page URL: https://fictional.invalid/about/careers/applications/jobs/results\n'
            '### Snapshot\n- link "Careers":\n  - /url: /about/careers/applications/\n'
            '- link "Software Engineer":\n  - /url: ' + href)
    store.agent_runtime_event(worker['id'], {'type': 'tool_result', 'tool_name': 'playwright.browser_snapshot',
        'success': True, 'session_id': store.get_agent_worker(worker['id'])['session_id'],
        'transport_event': {'type': 'item.completed', 'item': {'type': 'mcp_tool_call', 'server': 'playwright',
        'tool': 'browser_snapshot', 'status': 'completed', 'result': {'content': [{'type': 'text', 'text': text}]}}}})
    action = lambda: store.agent_action(worker['id'], launch['capability'], 'record_discovery_inventory',
        {'listings': [{'url': url, 'title': 'Software Engineer', 'requisition_id': '202'}]})
    if allowed:
        assert action()['unreviewed_listings'] == 1
    else:
        with pytest.raises(Conflict, match='absent'):
            action()


def test_scored_backlog_does_not_hide_failed_discovery_when_submission_is_paused(tmp_path):
    store, pipeline, discovery, _, _ = seed(tmp_path, 1)
    store.set_stage_dispatch(pipeline['id'], 'submission', True)
    evaluator = next(w for w in store.list_agent_workers(pipeline['id']) if w['role'] == 'evaluator')
    launch = start(store, evaluator)
    store.agent_action(evaluator['id'], launch['capability'], 'finish_evaluation', scored(88))
    store.finish_agent_turn(evaluator['id'], {'disposition': 'completed'})
    store.finish_agent_turn(discovery['id'], {'disposition': 'failed', 'error': 'Results page stayed empty.'})
    p = store.get_pipeline(pipeline['id'])
    assert p['stage'] == 'needs_attention'
    assert any('Results page stayed empty' in value for value in p['blockers'])
    assert store.agent_pipeline_counts(pipeline['id'])['scored_backlog'] == 1
    assert p['submission_paused'] and not p['evaluation_paused'] and not p['discovery_paused']


def test_published_total_alias_does_not_make_partial_discovery_look_fully_accounted(tmp_path):
    from app.discovery_inventory import accounting
    store, pipeline, _, _, _ = seed(tmp_path, 1)
    with store.connect() as db:
        result = accounting(db, pipeline['id'], {'published_listing_total': 3355})
    assert result['reported_listing_total'] == 3355
    assert result['unaccounted_listings'] == 3354
    assert not result['coverage_accounted'] and not result['coverage_verified']

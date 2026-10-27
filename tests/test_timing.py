import pytest

from app.timing import RunTiming, summarize_timings
from test_efficiency_controls import seed, start, cua


def test_parallel_tools_are_merged_and_residual_is_not_called_inference():
    now = [0.0]
    timer = RunTiming('one', clock=lambda: now[0])
    now[0] = 2; timer.model_start()
    now[0] = 3; timer.tool_start('a', 'cua_repl.js', 'browser')
    now[0] = 4; timer.tool_start('b', 'cua_repl.js', 'browser')
    now[0] = 5; timer.tool_start('c', 'agent_bridge.get_context', 'other')
    now[0] = 7; timer.tool_end('a')
    now[0] = 8; timer.tool_end('b', False)
    now[0] = 10; timer.tool_end('c')
    now[0] = 12; s = timer.summary()
    assert s['startup_seconds'] == 2
    assert s['browser_tool_seconds'] == 5
    assert s['other_tool_seconds'] == 5
    assert s['all_tool_seconds'] == 7
    assert s['tool_overlap_seconds'] == 3
    assert s['model_and_transport_seconds'] == 3
    assert s['pure_inference_seconds'] is None
    stats = summarize_timings([s, s])
    assert stats['measured_turns'] == 1 and stats['browser_calls'] == 2
    assert stats['browser_p95_seconds'] == 4


def test_interrupted_call_is_censored_and_repeated_completion_not_counted_twice():
    now = [0.0]; timer = RunTiming(clock=lambda: now[0]); timer.model_start()
    timer.observe({'type': 'item.started', 'item': {'type': 'mcp_tool_call', 'id': 'x', 'server': 'cua_repl', 'tool': 'js'}})
    now[0] = 3
    s = timer.summary(); assert s['incomplete_calls'] == 1 and s['browser_tool_seconds'] == 3
    assert summarize_timings([s])['browser_p95_seconds'] is None
    timer.observe({'type': 'item.completed', 'item': {'type': 'mcp_tool_call', 'id': 'x', 'status': 'completed', 'result': {}}})
    timer.tool_end('x'); assert len(timer.summary()['calls']) == 1


def test_metrics_persist_through_runtime_events_without_inventing_historical_timing(tmp_path):
    store, pipeline, discovery, _, _ = seed(tmp_path, 0)
    assert store.get_agent_efficiency(pipeline['id'])['timing']['status'] == 'unavailable'
    now = [0.0]; timer = RunTiming('persisted', clock=lambda: now[0]); timer.model_start()
    now[0] = 5
    event = {'type': 'timing_summary', 'timing': timer.summary()}
    store.agent_runtime_event(discovery['id'], event)
    store.agent_runtime_event(discovery['id'], event)
    result = store.get_agent_efficiency(pipeline['id'])['timing']
    assert result['measured_turns'] == 1 and result['model_and_transport_seconds'] == 5
    assert result['pure_inference_seconds'] is None


def test_authentication_redaction_keeps_numeric_timing_but_not_other_payload(tmp_path):
    import json
    store, pipeline, _, _, _ = seed(tmp_path, 1)
    evaluator = next(w for w in store.list_agent_workers(pipeline['id']) if w['role']=='evaluator')
    launch = start(store, evaluator)
    store.agent_action(evaluator['id'], launch['capability'], 'finish_evaluation', {'decision':'approve','reason':'Fixture'})
    store.finish_agent_turn(evaluator['id'], {'disposition':'completed'})
    store.authorize_agent_signin(pipeline['id'], 'candidate@example.test', 'Fictional permission')
    parent = next(w for w in store.list_agent_workers(pipeline['id']) if w['role']=='submission')
    launch = start(store, parent); cua(store, parent)
    store.agent_action(parent['id'], launch['capability'], 'browser_control', {'operation':'acquire_auth'})
    event = {'type':'timing_summary','timing':RunTiming('auth').summary(),'other':'fictional-otp-do-not-log'}
    store.agent_runtime_event(parent['id'], event)
    with store.connect() as db:
        raw = db.execute("SELECT payload FROM agent_runtime_events WHERE event_type='timing_summary'").fetchone()[0]
    assert json.loads(raw)['authentication_redacted'] is True
    assert 'fictional-otp-do-not-log' not in raw
    assert store.get_agent_efficiency(pipeline['id'])['timing']['measured_turns'] == 1

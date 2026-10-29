import copy
import json

import pytest

from app.provider_telemetry import scoped_tools,native_worker_tools,fingerprint
from app.agent_runtime import CodexAgentRuntime
from app.browser_sessions import BrowserSessions
from test_agent_pipeline_manager import _begin_isolated_discovery_turn, _record_unselected_discovery_listing
from test_agent_runtime import _runtime_root, _task


def test_provider_catalog_keeps_only_role_bridge_and_assigned_browser_schemas():
    catalog=[
        {'type':'function','name':'exec_command'},
        {'type':'function','name':'list_mcp_resources'},
        {'type':'namespace','name':'mcp__agent_bridge','tools':[
            {'type':'function','name':'get_context','parameters':{'type':'object'}},
            {'type':'function','name':'finish_evaluation'},
            {'type':'function','name':'finish_submission'}]},
        {'type':'namespace','name':'mcp__playwright','tools':[
            {'type':'function','name':'browser_snapshot'},
            {'type':'function','name':'browser_evaluate'}]},
        {'type':'namespace','name':'mcp__cua_repl','tools':[{'type':'function','name':'js'}]},
        {'type':'function','namespace':'other_server','name':'get_context'},
    ]
    original=copy.deepcopy(catalog)
    scoped=scoped_tools(catalog,'evaluator','playwright')
    assert [t['name'] for t in scoped]==['mcp__agent_bridge','mcp__playwright']
    assert [t['name'] for t in scoped[0]['tools']]==['get_context','finish_evaluation']
    assert [t['name'] for t in scoped[1]['tools']]==['browser_snapshot']
    assert scoped[0]['tools'][0]['parameters']=={'type':'object'}
    assert catalog==original
    assert [t['name'] for t in scoped_tools(catalog,'document','none')]==['mcp__agent_bridge']
    assert [t['name'] for t in scoped_tools(catalog,'submission','cua')]==['mcp__agent_bridge','mcp__cua_repl']


def test_flattened_mcp_names_are_supported_without_exposing_host_tools():
    tools=[{'type':'function','name':n} for n in ['mcp__agent_bridge__get_context','exec_command','mcp__playwright__browser_snapshot']]
    assert scoped_tools(tools,'evaluator','playwright')==[tools[0],tools[2]]


def test_cli_disables_shell_tools_and_routes_both_fresh_and_resumed_workers(tmp_path):
    runtime=CodexAgentRuntime(_runtime_root(tmp_path),'codex-fixture')
    for sid in (None,'saved-session'):
        task=_task('evaluator',session_id=sid)
        args=runtime._args(task['worker'],tmp_path/'schema.json',{})
        assert 'features.shell_tool=false' in args
        prompt=runtime._prompt(task,task['worker'])
        assert prompt.startswith('TOOL ROUTING: Your first workflow call is mcp__agent_bridge.get_context')
        assert 'first use the advertised tool_search' in prompt
        assert 'Source handles are bridge identifiers, not filesystem paths.' in prompt


def test_current_cli_deferred_catalog_retains_only_client_mcp_lookup():
    # Captured from the installed CLI against a local HTTP fixture, without
    # invoking a model. MCP schemas are loaded by this lookup, not preloaded.
    catalog=[{'type':'function','name':n} for n in ['list_mcp_resources','list_mcp_resource_templates',
        'read_mcp_resource','request_user_input','view_image','get_goal','create_goal','update_goal']]
    lookup={'type':'tool_search','execution':'client','parameters':{'type':'object'}}
    catalog += [{'type':'custom','name':'apply_patch'}, {'type':'namespace','name':'collaboration',
        'tools':[{'type':'function','name':'spawn_agent'}]},lookup]
    assert scoped_tools(catalog,'evaluator','playwright')==[lookup]
    loaded={'type':'namespace','name':'mcp__agent_bridge','tools':[{'type':'function','name':'get_context','defer_loading':True}]}
    assert scoped_tools([*catalog,loaded],'evaluator','playwright')==[lookup,loaded]


def test_direct_schemas_use_the_exact_pinned_bridge_and_browser_catalog(tmp_path):
    from app.worker_contracts import capture
    from app.efficiency_features import features
    worker={'id':'pinned-fixture','role':'evaluator','context':{'execution':{'browser_backend':'playwright'}}}
    contract=capture(tmp_path,worker,features(tmp_path));worker['context']['instruction_contract_id']=contract['id']
    browser=[{'name':'browser_snapshot','description':'Native fixture snapshot','inputSchema':{'type':'object','properties':{'depth':{'type':'number'}}}}]
    path=tmp_path/'data/worker-contracts'/worker['id']/(contract['id']+'.browser.json')
    path.write_text(json.dumps({'tools':browser,'sha256':fingerprint(browser)}))
    catalog=native_worker_tools(tmp_path,worker)
    assert [t['name'] for t in catalog]==['mcp__agent_bridge','mcp__playwright']
    assert 'get_context' in {t['name'] for t in catalog[0]['tools']}
    assert 'finish_submission' not in {t['name'] for t in catalog[0]['tools']}
    assert catalog[1]['tools'][0]['parameters']==browser[0]['inputSchema']
    assert all(t['type']=='namespace' for t in catalog)
    path.write_text(json.dumps({'tools':browser,'sha256':'tampered'}))
    with pytest.raises(ValueError,match='integrity'):native_worker_tools(tmp_path,worker)


def test_deepseek_prompt_routes_to_direct_mcp_without_native_tool_search(tmp_path):
    runtime=CodexAgentRuntime(_runtime_root(tmp_path),'codex-fixture');task=_task('evaluator')
    task['worker']['context']={'execution':{'provider':'deepseek','model':'deepseek-flash','effort':'high','browser_backend':'none'}}
    prompt=runtime._prompt(task,task['worker'])
    assert prompt.startswith('TOOL ROUTING: Your exact role MCP schemas are supplied directly.')
    assert 'Do not use tool_search' in prompt


@pytest.mark.parametrize('session_state,has_observation,expected',[
    ('live',True,'queued'),('lost',True,'interrupted'),('live',False,'interrupted'),
])
def test_playwright_discovery_continues_without_a_native_tab_id_only_with_owned_live_evidence(tmp_path,monkeypatch,session_state,has_observation,expected):
    from app.store import Store
    store=Store(tmp_path)
    p,w,launch=_begin_isolated_discovery_turn(store,'Fixture Playwright Continuation')
    with store.connect(write=True) as db:
        context=json.loads(db.execute('SELECT context FROM agent_workers WHERE id=?',(w['id'],)).fetchone()[0])
        context['execution']={'provider':'deepseek','model':'deepseek-flash','effort':'low','browser_backend':'playwright'}
        db.execute('UPDATE agent_workers SET context=? WHERE id=?',(json.dumps(context),w['id']))
    store.agent_action(w['id'],launch['capability'],'checkpoint',{'checkpoint':'Next search page',
        'context':{'current_page':2,'next_url':'https://amazon.fixture.example/search?page=2'}})
    _record_unselected_discovery_listing(store,w['id'],launch['capability'])
    monkeypatch.setattr(BrowserSessions,'inspect',lambda self,wid: {'state':session_state,'worker_id':wid})
    if has_observation:
        store.agent_runtime_event(w['id'],{'type':'tool_result','tool_name':'playwright.browser_snapshot','success':True,
            'session_id':'fixture-saved-session','transport_event':{'item':{'type':'mcp_tool_call','server':'playwright',
            'tool':'browser_snapshot','result':{'content':[{'type':'text','text':'### Page\n- Page URL: https://amazon.fixture.example/search'}]}}}})
    result=store.agent_action(w['id'],launch['capability'],'finish_discovery',{
        'status':'incomplete','detail':'One new JD recorded; more pages remain.','coverage':{'current_page':2}})
    assert result['continuation']['state']==expected
    store.finish_agent_turn(w['id'],{'disposition':'waiting','session_id':'fixture-saved-session'})
    worker=store.get_agent_worker(w['id'])
    assert worker['state']==expected and worker['session_id']=='fixture-saved-session'
    assert 'tab_id' not in worker['context']

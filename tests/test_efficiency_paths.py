import copy
import json
from pathlib import Path
import shutil

import pytest

from app.agent_runtime import CodexAgentRuntime
from app.store import Store,Conflict
from tools.agent_mcp import AgentMCPBridge
from tools.browser_mcp import BrowserBridge
from test_agent_pipeline_manager import _create_document_parent

ROOT=Path(__file__).resolve().parents[1]


class RPC:
    def __init__(self,fail=False):self.calls=[];self.fail=fail
    def call(self,name,args):
        self.calls.append((name,args))
        if name=='browser_fill_form':return {'isError':self.fail,'content':[{'type':'text','text':'partial failure' if self.fail else 'raw generated fill code'}]}
        return {'content':[{'type':'text','text':'### Page\n- Page URL: https://fictional.invalid/apply\n### Snapshot\n- textbox "Name" [ref=e1]: Test Candidate'}]}


def browser(fail=False):
    b=BrowserBridge.__new__(BrowserBridge);b.rpc=RPC(fail);b.catalog=[{'name':'browser_fill_form'}]
    b.settings={'batch_form_edits':True};b.role='submission';return b


def test_batched_fields_return_current_observation_without_model_read_roundtrip():
    b=browser();r=b.call('browser_fill_form',{'fields':[{'type':'textbox','target':'e1','name':'Name','value':'Test Candidate'}]})
    assert [n for n,_ in b.rpc.calls]==['browser_fill_form','browser_snapshot']
    assert 'Test Candidate' in r['content'][-1]['text'] and not r['isError']
    assert 'raw generated fill code' not in json.dumps(r)


def test_mismatched_job_and_binding_controls_are_not_batched():
    b=browser();fields=[{'type':'textbox','target':'e1','name':'Name','value':'Test Candidate'}]
    r=b.call('browser_fill_form',{'expected_url':'https://other.invalid/apply','fields':fields})
    assert r['isError'] and all(n!='browser_fill_form' for n,_ in b.rpc.calls)
    with pytest.raises(ValueError,match='separate observed action'):
        b.call('browser_fill_form',{'fields':[{'type':'checkbox','target':'e2','name':'Agree','value':'true'}]})


def test_partial_batch_failure_returns_live_state_without_automatic_retry():
    b=browser(True);r=b.call('browser_fill_form',{'fields':[{'type':'textbox','target':'e1','name':'Name','value':'Test Candidate'}]})
    assert r['isError'] and 'earlier fields may have changed' in r['content'][0]['text']
    assert [n for n,_ in b.rpc.calls].count('browser_fill_form')==1


def test_compact_prompt_prefix_and_resume_do_not_repeat_native_adapter(tmp_path,monkeypatch):
    (tmp_path/'config').mkdir();shutil.copytree(ROOT/'prompts',tmp_path/'prompts')
    (tmp_path/'config/workers.json').write_text((ROOT/'config/workers.json').read_text())
    runtime=CodexAgentRuntime(tmp_path,codex_bin='fixture')
    def prompt(compact,identifier='one',session=None):
        (tmp_path/'config/efficiency.json').write_text(json.dumps({'compact_worker_context':compact}))
        w={'id':identifier,'role':'submission','session_id':session,'context':{'execution':{'browser_backend':'playwright','provider':'deepseek','model':'deepseek-flash','effort':'low'}}}
        return runtime._prompt({'worker':w,'task_context':{'applicant_index':{'known':'same factual depth'},'job':{'id':identifier}}},w)
    old=prompt(False);new=prompt(True)
    assert len(new)<len(old)*.65
    assert new.split('DURABLE WORKER STATE')[0]==prompt(True,'two').split('DURABLE WORKER STATE')[0]
    resumed=prompt(True,session='saved')
    assert len(resumed)<len(prompt(False,session='saved'))*.5
    assert 'Native tab groups' not in resumed
    (tmp_path/'config/efficiency.json').write_text(json.dumps({'compact_worker_context':True}))
    monkeypatch.setattr('app.browser_backends.playwright_args',lambda *_:[])
    w={'id':'one','role':'submission','context':{'execution':{'browser_backend':'playwright','provider':'deepseek','model':'deepseek-flash','effort':'low'}}}
    assert 'features.apps=false' in runtime._args(w,tmp_path/'schema.json',{})
    w['context']['execution'].update(browser_backend='cua',provider='openai',model='gpt-6-luna')
    assert 'features.apps=false' not in runtime._args(w,tmp_path/'schema.json',{})


def test_source_reuse_is_one_invocation_only_and_never_masks_stale_scope(tmp_path):
    source={'id':'source:fixture','content':'Exact factual depth. '*200,'sha256':'actual-source-hash'};calls=[]
    def handler(w,c,n,a):calls.append(a);return {'sources':[copy.deepcopy(source)]}
    bridge=AgentMCPBridge('worker','capability','document',handler,root=tmp_path)
    assert bridge.call('retrieve_sources',{'source_ids':['source:fixture']})['sources'][0]['content']
    assert bridge.call('retrieve_sources',{'source_ids':['source:fixture']})['sources'][0]['unchanged_this_invocation']
    assert bridge.call('retrieve_sources',{'source_ids':['source:fixture'],'force':True})['sources'][0]['content']
    assert all('force' not in a for a in calls)
    source['content']='Changed exact source.'
    assert bridge.call('retrieve_sources',{'source_ids':['source:fixture']})['sources'][0]['content']==source['content']
    bridge._action_handler=lambda *a:(_ for _ in ()).throw(Conflict('stale snapshot'))
    with pytest.raises(Conflict,match='stale'):bridge.call('retrieve_sources',{'source_ids':['source:fixture']})


def test_document_grouped_edits_preserve_immutable_source_and_scope(tmp_path):
    s=Store(tmp_path);p,parent,child=_create_document_parent(s)
    assert s.reserve_agent_worker(child['id']);launch=s.agent_launch_context(child['id']);cap=launch['capability']
    original='# Test Candidate\n\n## Summary\nConfirmed C++ work.\n\n## Experience\nMarch 2022 - May 2026.\n'
    pub=s.agent_action(child['id'],cap,'publish_documents',{'artifacts':[{'kind':'resume','markdown':original}]})
    a=pub['artifacts'][0];request={'edits':[{'kind':'resume','base_source_id':'artifact:'+a['id'],'expected_sha256':a['sha256'],'replacements':[{'old':'Confirmed C++ work.','new':'Confirmed C++ systems work.'}]}]}
    changed=s.agent_action(child['id'],cap,'edit_documents',request)['artifacts'][0]
    assert (tmp_path/a['path']).read_text()==original
    assert (tmp_path/changed['path']).read_text()==original.replace('C++ work','C++ systems work')
    assert changed['version']==2
    request['edits'][0]['expected_sha256']='wrong'
    with pytest.raises(Conflict,match='source hash'):s.agent_action(child['id'],cap,'edit_documents',request)
    request['edits'][0]['expected_sha256']=a['sha256'];request['edits'][0]['replacements'][0]['old']='Absent passage'
    with pytest.raises(Conflict,match='absent or ambiguous'):s.agent_action(child['id'],cap,'edit_documents',request)
    with s.connect() as db:assert db.execute('SELECT count(*) FROM agent_artifacts WHERE worker_id=?',(child['id'],)).fetchone()[0]==2

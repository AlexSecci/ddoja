"""Bounded DeepSeek/CLI token ablations using synthetic text and a read-only ping.

No production Store, browser, mail, application or sign-in state is used. Keys
are supplied only through the child environment, never argv or report files.
"""
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import argparse
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from app.agent_runtime import CodexAgentRuntime
from app.provider_credentials import ProviderCredentials

OUT=ROOT/'data/experiments/five-area-improvements'
RESUME=False


def run(root,runtime,worker,prompt,schema,label):
    recorded=OUT/(label+'.json')
    if RESUME and recorded.exists():
        value=json.loads(recorded.read_text(encoding='utf-8'))
        print(json.dumps({'label':label,'reused_recorded_measurement':True}),flush=True)
        return value
    path=root/(label+'-schema.json');path.write_text(json.dumps(schema),encoding='utf-8')
    env={'AGENT_ROOT':str(root),'AGENT_WORKER_ID':worker['id'],'AGENT_ROLE':worker['role'],
         'AGENT_CAPABILITY':'fictional-smoke-only','AGENT_BRIDGE_SMOKE':'1'}
    with patch('app.browser_backends.playwright_args',lambda *_:[]):args=runtime._args(worker,path,env)
    process_env={**os.environ,**env,'DEEPSEEK_API_KEY':ProviderCredentials(ROOT).get()}
    if not process_env['DEEPSEEK_API_KEY']:raise RuntimeError('The existing DeepSeek credential is unavailable.')
    started=time.monotonic();p=subprocess.Popen(args,cwd=root,env=process_env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
        creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP|subprocess.CREATE_NO_WINDOW) if os.name=='nt' else 0)
    stdout,stderr=p.communicate(prompt.encode(),timeout=600)
    events=[]
    for line in stdout.decode('utf-8',errors='replace').splitlines():
        try:events.append(json.loads(line))
        except ValueError:pass
    usage=next((e.get('usage') for e in reversed(events) if e.get('type')=='turn.completed'),None)
    messages=[e['item']['text'] for e in events if e.get('type')=='item.completed' and e.get('item',{}).get('type')=='agent_message']
    tools=[(e['item'].get('server'),e['item'].get('tool')) for e in events if e.get('type')=='item.started' and e.get('item',{}).get('type')=='mcp_tool_call']
    if p.returncode or not usage or not messages:
        raise RuntimeError('Synthetic model measurement did not complete; no production recovery was attempted. '+str({'returncode':p.returncode,'event_types':[e.get('type') for e in events][-5:]}))
    if any(server!='agent_bridge' or name!='ping' for server,name in tools):raise RuntimeError('The fixture used a tool outside its single read-only ping scope.')
    result=json.loads(messages[-1]);report={'label':label,'elapsed_seconds':time.monotonic()-started,'input_tokens':usage['input_tokens'],
        'cached_input_tokens':usage.get('cached_input_tokens'),'output_tokens':usage['output_tokens'],
        'reasoning_tokens':usage.get('reasoning_output_tokens'),'tool_calls':tools,'prompt_bytes':len(prompt.encode()),'output_bytes':len(messages[-1].encode()),'result':result}
    (OUT/(label+'.json')).write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k!='result'}),flush=True);return report


def main():
    OUT.mkdir(parents=True,exist_ok=True);root=Path(tempfile.mkdtemp(prefix='ddoja-model-ablation-'))
    (root/'config').mkdir();(root/'tools').mkdir();shutil.copytree(ROOT/'prompts',root/'prompts')
    shutil.copy2(ROOT/'tools/agent_mcp.py',root/'tools/agent_mcp.py');shutil.copy2(ROOT/'config/workers.json',root/'config/workers.json');shutil.copy2(ROOT/'config/deepseek-models.json',root/'config/deepseek-models.json')
    runtime=CodexAgentRuntime(root);worker={'id':'synthetic-context-measurement','role':'submission','context':{'execution':{'provider':'deepseek','model':'deepseek-flash','effort':'low','browser_backend':'playwright','transport':'exec'}}}
    task={'worker':worker,'task_context':{'applicant_index':{'confirmed':'Test Candidate, fictional profile only; C++ studied, no work experience'},'job':{'title':'Fictional C++ role','jd':'Synthetic C++ role for an isolated token measurement. No employer application exists.'}}}
    schema={'type':'object','properties':{k:{'type':'string'} for k in ['disposition','checkpoint','summary']},'required':['disposition','checkpoint','summary'],'additionalProperties':False}
    reports=[]
    for compact in [False,True]:
        (root/'config/efficiency.json').write_text(json.dumps({'compact_worker_context':compact}))
        prompt=runtime._prompt(task,worker)+'\n\nSYNTHETIC HARNESS OVERRIDE: This is a token/capability fixture, not a live worker. No dashboard, browser or employer exists. Only the read-only agent_bridge.ping tool is exposed. Call ping once, then return the required JSON with disposition completed and checkpoint fixture_verified. Do not attempt any application workflow or other tool.'
        reports.append(run(root,runtime,worker,prompt,schema,'context-'+str(compact)))
    # For the edit encoding comparison, all other settings and exact inputs stay
    # fixed. The only requested difference is full publication vs exact edits.
    (root/'config/efficiency.json').write_text(json.dumps({'compact_worker_context':True}))
    worker=copy.deepcopy(worker);worker['role']='document';worker['context']['execution'].update(effort='high',browser_backend='none')
    original='# Test Candidate\n\n## Summary\nConfirmed C++ work.\n\n## Experience\nFictional Company | March 2022 - May 2026\n'+''.join(f'- Synthetic evidence {i}: implemented and tested C++ services with explicit interfaces, bounded queues, reproducible logs and documented operational behavior.\n' for i in range(25))
    expected=original.replace('Confirmed C++ work.','Confirmed C++ systems work.')
    for edit in [False,True]:
        schema={'type':'object','properties':{'markdown':{'type':'string'},'replacements':{'type':'array','items':{'type':'object','properties':{'old':{'type':'string'},'new':{'type':'string'}},'required':['old','new'],'additionalProperties':False}}},'required':['markdown','replacements'],'additionalProperties':False}
        instructions=('Return markdown as the entire revised document and replacements as an empty array.' if not edit else 'Return markdown as an empty string and replacements as the single exact old/new replacement. Do not repeat unchanged document text.')
        prompt=('Synthetic document-edit encoding measurement. No real applicant, employer or approval exists. Do not use tools. '
                'Make exactly one factual-preserving edit: replace "Confirmed C++ work." with "Confirmed C++ systems work." '
                'Every other character of the document must remain unchanged. '+instructions+'\nSOURCE MARKDOWN:\n'+original)
        result=run(root,runtime,worker,prompt,schema,'document-edit-'+str(edit));reports.append(result)
        value=result['result'];actual=value['markdown']
        if edit:
            actual=original
            for change in value['replacements']:
                assert actual.count(change['old'])==1;actual=actual.replace(change['old'],change['new'],1)
        # A terminating LF has no visible/document meaning; every actual line
        # must otherwise be identical. Do not normalize arbitrary whitespace.
        assert actual.rstrip('\n')==expected.rstrip('\n'),'The comparison is invalid unless both encodings produce identical content.'
        result['identical_document_lines']=True
        result['normalization']='Ignore only terminating LF; all document lines are otherwise exact.'
    (OUT/'model-ablations.json').write_text(json.dumps({'method':'actual DeepSeek through same Codex CLI; isolated synthetic workloads; each pair changes one area; no production tasks','cases':reports},indent=2),encoding='utf-8')


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--resume',action='store_true');args=parser.parse_args();RESUME=args.resume;main()

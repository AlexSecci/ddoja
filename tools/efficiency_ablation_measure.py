"""Independent structural and scheduler measurements; isolated data only."""
from concurrent.futures import ThreadPoolExecutor
import copy
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import time

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
from app.agent_runtime import CodexAgentRuntime
from app.browser_backends import playwright_status
from app.browser_sessions import BrowserSessions
from app.browser_transport import BrowserRPC
from tools.agent_mcp import AgentMCPBridge
from tools.browser_efficiency_acceptance import Page,ThreadingHTTPServer,text,ref
from test_isolated_form_slots import prepared

OUT=ROOT/'data/experiments/five-area-improvements'


def context():
    root=Path(tempfile.mkdtemp(prefix='ddoja-context-ablation-'));(root/'config').mkdir();shutil.copytree(ROOT/'prompts',root/'prompts')
    shutil.copy2(ROOT/'config/workers.json',root/'config/workers.json')
    runtime=CodexAgentRuntime(root,codex_bin='not-invoked');results=[]
    for enabled in [False,True]:
        (root/'config/efficiency.json').write_text(json.dumps({'compact_worker_context':enabled}))
        w={'id':'fixture-a','role':'submission','context':{'execution':{'provider':'deepseek','model':'deepseek-flash','effort':'low','browser_backend':'playwright'}}}
        task={'worker':w,'task_context':{'applicant_index':{'confirmed':'Fictional C++ knowledge; no production Kubernetes'},'job':{'id':'fictional','jd':'Complete fictional JD. C++ services; optional Kubernetes.'}}}
        launch=runtime._prompt(task,w);changed=copy.deepcopy(w);changed['id']='fixture-b';changed_task=copy.deepcopy(task);changed_task['worker']=changed;changed_task['task_context']['job']['id']='other'
        stable=launch.split('DURABLE WORKER STATE')[0]==runtime._prompt(changed_task,changed).split('DURABLE WORKER STATE')[0]
        w['session_id']='fictional-saved';resume=runtime._prompt(task,w)
        def handler(*_):return {'sources':[{'id':'exact-source','content':'Full immutable factual evidence. '*1000,'sha256':'source-sha'}]}
        bridge=AgentMCPBridge('fixture','fixture-cap','document',handler,root=root)
        source_bytes=sum(len(json.dumps(bridge.call('retrieve_sources',{'source_ids':['exact-source']})).encode()) for _ in range(3))
        restored=bridge.call('retrieve_sources',{'source_ids':['exact-source'],'force':True})['sources'][0]['content']
        results.append({'compact_worker_context':enabled,'launch_bytes':len(launch.encode()),'resume_bytes':len(resume.encode()),'three_source_results_bytes':source_bytes,'full_source_restorable':restored=='Full immutable factual evidence. '*1000,'stable_prefix_across_jobs':stable,'prefix_sha256':hashlib.sha256(launch.split('DURABLE WORKER STATE')[0].encode()).hexdigest()})
    value={'method':'identical scoped synthetic inputs; one feature changed; bytes are not tokens','cases':results}
    (OUT/'context-structural.json').write_text(json.dumps(value,indent=2));return value


def slots():
    status=playwright_status(ROOT);server=ThreadingHTTPServer(('127.0.0.1',0),Page);threading.Thread(target=server.serve_forever,daemon=True).start();results=[]
    try:
        for enabled in [False,True]:
            root=Path(tempfile.mkdtemp(prefix='ddoja-slots-ablation-'));s,p,workers=prepared(root,enabled=enabled);workers=workers[:2];registry=BrowserSessions(root)
            try:
                endpoints={}
                for w in workers:
                    endpoints[w['id']]=registry.ensure(w['id'],status)['endpoint']
                    with BrowserRPC(endpoints[w['id']]) as client:client.call('browser_tabs',{'action':'list'})
                active=0;peak=0;lock=threading.Lock();checks=[]
                def prepare(w):
                    nonlocal active,peak
                    with lock:active+=1;peak=max(peak,active)
                    try:
                        with BrowserRPC(endpoints[w['id']]) as client:
                            client.call('browser_navigate',{'url':f'http://127.0.0.1:{server.server_port}/delayed-fictional'})
                            snapshot=text(client.call('browser_snapshot',{}));client.call('browser_fill_form',{'fields':[{'name':'First name','type':'textbox','target':ref(snapshot,'First name'),'value':w['id']}]})
                            observed=text(client.call('browser_snapshot',{}));assert w['id'] in observed
                            checks.append(True)
                    finally:
                        with lock:active-=1
                        s.finish_agent_turn(w['id'],{'disposition':'interrupted','checkpoint':'synthetic preparation only; no application send'})
                start=time.monotonic();pending=list(workers);futures=[]
                with ThreadPoolExecutor(max_workers=2) as pool:
                    while pending:
                        for w in list(pending):
                            if s.reserve_agent_worker(w['id']):pending.remove(w);futures.append(pool.submit(prepare,w))
                        for f in futures:
                            if f.done():f.result()
                        if pending:time.sleep(.01)
                    for f in futures:f.result()
                results.append({'isolated_form_slots':enabled,'wall_seconds':time.monotonic()-start,'peak_simultaneous_preparations':peak,'same_two_existing_workers':True,'values_verified':len(checks)==2,'injected_site_latency_seconds':.20})
            finally:
                for w in workers:
                    if registry.inspect(w['id'])['state']=='live':registry.close(w['id'],force=True)
    finally:server.shutdown()
    value={'method':'actual scheduler reservations + two isolated Chromium form preparations; same pool size 2; fixed 200ms local page latency, only isolation switch differs','cases':results}
    (OUT/'form-slots.json').write_text(json.dumps(value,indent=2));return value


if __name__=='__main__':
    OUT.mkdir(parents=True,exist_ok=True)
    print(json.dumps({'context':context(),'slots':slots()}))

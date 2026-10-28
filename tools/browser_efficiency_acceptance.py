"""Independent real-browser ablations on a synthetic local form; no inference/employer."""
import argparse
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
from pathlib import Path
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from app.browser_backends import playwright_status
from app.browser_sessions import BrowserSessions
from app.browser_transport import BrowserRPC

HTML='''<!doctype html><html><body><h1>Fictional software application</h1>
<form><label>First name<input name="first"></label><label>Email<input name="email" type="email"></label>
<label>Notes<textarea name="notes"></textarea></label><label>Resume<input name="resume" type="file"></label>
<label>Country<select name="country"><option>Italy</option><option>United States</option></select></label>
<p id="status">Unsaved draft</p><button type="button" onclick="document.getElementById('status').innerText='Local test only'">Save local draft</button></form>
<script>window.unsavedNonce='retained-'+Math.random();window.inputEvents=0;document.addEventListener('input',()=>window.inputEvents++);</script></body></html>'''


class Page(BaseHTTPRequestHandler):
    def do_GET(self):
        if 'delayed' in self.path:time.sleep(.20)
        self.send_response(200);self.send_header('Content-Type','text/html');self.end_headers();self.wfile.write(HTML.encode())
    def log_message(self,*args):pass
    def handle(self):
        try:super().handle()
        except ConnectionResetError:pass  # Normal browser shutdown in the fixture.


class StdioMCP:
    def __init__(self,args,cwd):
        self.p=subprocess.Popen(args,cwd=cwd,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
        self.messages=queue.Queue();self.sequence=0;self.calls=[]
        def pump():
            for line in self.p.stdout:
                try:self.messages.put(json.loads(line))
                except ValueError:pass
        threading.Thread(target=pump,daemon=True).start()
        self.request('initialize',{'protocolVersion':'2024-11-05','capabilities':{},'clientInfo':{'name':'ddoja-fixture','version':'1'}})
        self.request('notifications/initialized',{},notify=True)

    def request(self,method,params,notify=False):
        self.sequence+=1;body={'jsonrpc':'2.0','method':method,'params':params}
        if not notify:body['id']=self.sequence
        self.p.stdin.write((json.dumps(body)+'\n').encode());self.p.stdin.flush()
        if notify:return {}
        deadline=time.monotonic()+60
        while True:
            message=self.messages.get(timeout=max(.01,deadline-time.monotonic()))
            if message.get('id')!=self.sequence:continue
            if 'error' in message:raise RuntimeError(message['error'])
            return message['result']

    def call(self,name,args):
        started=time.monotonic()
        result=self.request('tools/call',{'name':name,'arguments':args})
        self.calls.append({'tool':name,'elapsed_seconds':time.monotonic()-started,'response_characters':len(json.dumps(result))})
        if result.get('isError'):raise RuntimeError(result)
        return result

    def close(self):
        self.p.stdin.close()
        try:self.p.wait(timeout=10)
        except subprocess.TimeoutExpired:self.p.terminate();self.p.wait(timeout=5)


def text(result):return '\n'.join(b.get('text','') for b in result.get('content',[]))


def ref(snapshot,label,kind='textbox'):
    match=re.search(r'- '+kind+r' "'+re.escape(label)+r'"[^\n]*\[ref=([^\]]+)\]',snapshot)
    if not match:raise AssertionError('Observed element missing: '+label)
    return match.group(1)


def populate(client,url,file):
    client.call('browser_navigate',{'url':url});snapshot=text(client.call('browser_snapshot',{}))
    client.call('browser_fill_form',{'fields':[{'name':'First name','type':'textbox','target':ref(snapshot,'First name'),'value':'Test Candidate'},
        {'name':'Email','type':'textbox','target':ref(snapshot,'Email'),'value':'fixture@example.invalid'},
        {'name':'Notes','type':'textbox','target':ref(snapshot,'Notes'),'value':'Unsaved fictional notes'}]})
    client.call('browser_click',{'element':'Resume file field','target':ref(snapshot,'Resume','button')})
    client.call('browser_file_upload',{'paths':[str(file)]})


def inspect(client):
    # Test-only DOM readback through the actual Playwright tool. This capability
    # is not exposed to worker agents by the production proxy allowlist.
    result=client.call('browser_evaluate',{'function':"() => ({first:document.querySelector('[name=first]')?.value||'',email:document.querySelector('[name=email]')?.value||'',notes:document.querySelector('[name=notes]')?.value||'',country:document.querySelector('[name=country]')?.value||'',file:document.querySelector('[name=resume]')?.files[0]?.name||'',nonce:window.unsavedNonce||null})"})
    raw=text(result).split('### Result\n',1)[-1].split('\n###',1)[0].strip()
    return json.loads(raw)


def lifetime(output):
    status=playwright_status(ROOT);root=Path(tempfile.mkdtemp(prefix='ddoja-lifetime-ablation-'));resume=root/'fictional-resume.txt';resume.write_text('Test Candidate. Synthetic only.')
    server=ThreadingHTTPServer(('127.0.0.1',0),Page);threading.Thread(target=server.serve_forever,daemon=True).start();url=f'http://127.0.0.1:{server.server_port}/fictional'
    registry=BrowserSessions(root);data={'root':str(root),'method':'real Chromium, identical fictional form, no model calls'}
    try:
        baseline=root/'baseline.json';baseline.write_text(json.dumps({'browser':{'browserName':'chromium','userDataDir':str(root/'stdio-profile'),'launchOptions':{'executablePath':status['browser'],'headless':True}}}))
        args=[status['node'],status['cli'],'--config',str(baseline)]
        client=StdioMCP(args,root);populate(client,url,resume);before=inspect(client);baseline_calls=list(client.calls);client.close()
        client=StdioMCP(args,root)
        try:
            after=inspect(client)
            populate(client,url,resume)
            assert inspect(client)['file']==resume.name
            baseline_calls+=client.calls
        finally:client.close()
        assert before['first']=='Test Candidate' and before['file']==resume.name and after['first']=='' and after['file']==''
        data['baseline']={'values_retained':False,'file_retained':False,'upload_calls_for_one_resume':sum(c['tool']=='browser_file_upload' for c in baseline_calls),'calls':baseline_calls}
        owned=registry.ensure('persistent-owner',status)
        adapter=[sys.executable,str(ROOT/'tools/browser_mcp.py'),'--root',str(root),'--worker','persistent-owner','--role','submission']
        client=StdioMCP(adapter,root);populate(client,url,resume);persistent_calls=list(client.calls)
        with BrowserRPC(owned['endpoint']) as readback:before=inspect(readback)
        client.close()
        # A different host registry object models a dashboard/worker restart.
        reattached=BrowserSessions(root).ensure('persistent-owner',status)
        client=StdioMCP(adapter,root);client.call('browser_snapshot',{})
        persistent_calls+=client.calls
        # An abrupt CLI-side shutdown models quota/user-stop interruption.
        client.p.terminate();client.p.wait(timeout=5)
        with BrowserRPC(reattached['endpoint']) as readback:after=inspect(readback)
        assert before==after and reattached['pid']==owned['pid'] and reattached['generation']==owned['generation']
        other=registry.ensure('different-owner',status)
        with BrowserRPC(other['endpoint']) as client:isolated=inspect(client)
        assert isolated['first']=='' and isolated['file']==''
        data['improved']={'values_retained':True,'file_retained':True,'transient_js_nonce_retained':True,'same_live_process':True,'separate_owner_isolated':True,'survived_cli_termination':True,'upload_calls_for_one_resume':sum(c['tool']=='browser_file_upload' for c in persistent_calls),'calls':persistent_calls}
        data['passed']=True
    finally:
        server.shutdown()
        for wid in ('persistent-owner','different-owner'):
            if registry.inspect(wid)['state']=='live':registry.close(wid,force=True)
    output.parent.mkdir(parents=True,exist_ok=True);output.write_text(json.dumps(data,indent=2),encoding='utf-8');return data


def batching(output):
    root=Path(tempfile.mkdtemp(prefix='ddoja-batching-ablation-'));(root/'config').mkdir();status=playwright_status(ROOT)
    server=ThreadingHTTPServer(('127.0.0.1',0),Page);threading.Thread(target=server.serve_forever,daemon=True).start();url=f'http://127.0.0.1:{server.server_port}/fictional'
    registry=BrowserSessions(root);results=[]
    try:
        for enabled in [False,True]:
            (root/'config/efficiency.json').write_text(json.dumps({'batch_form_edits':enabled,'compact_worker_context':False}))
            wid='batch-'+str(enabled);owned=registry.ensure(wid,status)
            client=StdioMCP([sys.executable,str(ROOT/'tools/browser_mcp.py'),'--root',str(root),'--worker',wid,'--role','submission'],root)
            try:
                client.call('browser_navigate',{'url':url});snapshot=text(client.call('browser_snapshot',{}));start=len(client.calls)
                args={'fields':[{'name':label,'type':'textbox','target':ref(snapshot,label),'value':value} for label,value in [('First name','Test Candidate'),('Email','fixture@example.invalid'),('Notes','Synthetic notes')]]}
                if enabled:args['expected_url']=url
                result=client.call('browser_fill_form',args)
                if not enabled:result=client.call('browser_snapshot',{})
                with BrowserRPC(owned['endpoint']) as reader:values=inspect(reader)
                assert values['first']=='Test Candidate' and values['email']=='fixture@example.invalid' and values['notes']=='Synthetic notes'
                assert 'fixture@example.invalid' in text(result)
                calls=client.calls[start:];results.append({'batch_form_edits':enabled,'model_facing_calls':len(calls),'response_characters':sum(c['response_characters'] for c in calls),'browser_seconds':sum(c['elapsed_seconds'] for c in calls),'values_verified':True,'calls':calls})
            finally:client.close()
    finally:
        server.shutdown()
        for enabled in [False,True]:
            wid='batch-'+str(enabled)
            if registry.inspect(wid)['state']=='live':registry.close(wid,force=True)
    result={'method':'same three mapped fields; only batch_form_edits changed; actual Chromium; no inference','passed':True,'cases':results}
    output.parent.mkdir(parents=True,exist_ok=True);output.write_text(json.dumps(result,indent=2),encoding='utf-8');return result


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--area',choices=['lifetime','batching'],default='lifetime');parser.add_argument('--output',type=Path,required=True);options=parser.parse_args()
    print(json.dumps((lifetime if options.area=='lifetime' else batching)(options.output)))

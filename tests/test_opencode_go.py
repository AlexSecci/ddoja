from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import sqlite3
import threading
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from app import agent_runtime, provider_credentials
from app.inference_providers import go_models, write_go_cli_catalog
from app.provider_credentials import ProviderCredentials
from app.provider_telemetry import ProviderTelemetry
from app.request_metrics import diagnostics
from app.server import create_app
from app.store import Store
from test_agent_runtime import _runtime_root, _task, _FakeProcess
from test_efficiency_controls import seed
from test_ui_contract import IsolatedAgentManager


def test_go_catalog_uses_reported_efforts_and_restricts_visual_document_review(tmp_path):
    s=Store(tmp_path);settings=s.get_worker_settings();roles=deepcopy(settings['roles'])
    models={m['id']:m for m in go_models(tmp_path)}
    assert models['deepseek-v4.1-flash']['efforts']==['low','high','max']
    assert models['glm-5.2']['efforts']==['high','max']
    assert models['kimi-k2.7-code']['efforts']==['default']
    roles['evaluation']={'provider':'opencode_go','model':'glm-5.2','effort':'high','browser_backend':'playwright','service_tier':'default'}
    assert s.save_worker_settings(0,roles)['roles']['evaluation']['provider']=='opencode_go'
    roles['evaluation']['effort']='low'
    with pytest.raises(ValueError,match='reasoning effort'):s.save_worker_settings(1,roles)
    roles['evaluation']['effort']='high';roles['materials']={**roles['evaluation'],'browser_backend':'none'}
    with pytest.raises(ValueError,match='image-capable'):s.save_worker_settings(1,roles)
    catalog=json.loads(write_go_cli_catalog(tmp_path,tmp_path).read_text())['models']
    assert next(m for m in catalog if m['slug']=='glm-5.2')['input_modalities']==['text']
    assert next(m for m in catalog if m['slug']=='deepseek-v4.1-flash')['context_window']==1000000


def test_provider_keys_are_independent_encrypted_and_not_returned(tmp_path,monkeypatch):
    monkeypatch.setattr(provider_credentials,'_dpapi',lambda value,decrypt=False:value[::-1])
    app=create_app(tmp_path,agent_manager_factory=IsolatedAgentManager)
    with TestClient(app) as client:
        assert client.put('/api/settings/providers/opencode_go/key',json={'api_key':'synthetic-go-secret'}).status_code==403
        client.headers['X-Pipeline-Token']=client.get('/api/session').json()['token']
        for provider in ['deepseek','opencode_go']:
            secret='synthetic-'+provider+'-secret'
            r=client.put('/api/settings/providers/'+provider+'/key',json={'api_key':secret})
            assert r.status_code==200 and r.json()['configured']
            assert secret not in r.text+client.get('/api/settings/workers').text
            assert secret.encode() not in ProviderCredentials(tmp_path,provider).path.read_bytes()
        assert ProviderCredentials(tmp_path).get()=='synthetic-deepseek-secret'
        assert ProviderCredentials(tmp_path,'opencode_go').get()=='synthetic-opencode_go-secret'
        client.delete('/api/settings/providers/opencode_go/key')
        assert ProviderCredentials(tmp_path).status()['configured']
        assert app.state.agent_pipeline_manager.schedule_calls==0


def test_go_cli_uses_its_own_key_catalog_and_preserves_subscription_login(tmp_path,monkeypatch):
    root=_runtime_root(tmp_path)
    monkeypatch.setattr(agent_runtime.subprocess,'Popen',_FakeProcess)
    monkeypatch.setattr(ProviderCredentials,'get',lambda self:'synthetic-go-private-key')
    monkeypatch.setenv('DEEPSEEK_API_KEY','must-not-be-inherited')
    runtime=agent_runtime.CodexAgentRuntime(root,'codex-fixture',login_checker=lambda *_:pytest.fail('API provider must not change ChatGPT login'))
    task=_task(role='document');task['worker']['context']={'execution':{
        'provider':'opencode_go','model':'deepseek-v4.1-flash','effort':'high','browser_backend':'none','transport':'exec'}}
    assert runtime.run(task,lambda event:None,lambda:False)['disposition']=='completed'
    p=_FakeProcess.instances[-1];args=' '.join(p.args)
    assert 'model_provider="opencode_go"' in args and 'wire_api="responses"' in args
    assert 'forced_login_method' not in args and 'synthetic-go-private-key' not in args
    assert p.env['OPENCODE_API_KEY']=='synthetic-go-private-key' and 'DEEPSEEK_API_KEY' not in p.env
    assert 'features.fast_mode=false' in args
    for run in (root/'data/agent-runtime').iterdir():
        if (run/'opencode-go-models.json').exists():break
    else:pytest.fail('A per-invocation Go model catalog was not saved')


@pytest.mark.parametrize('stream',[False,True])
def test_go_relay_preserves_sessions_default_effort_and_actual_allowance(tmp_path,stream):
    s,p,_,_,entries=seed(tmp_path,1)
    with s.connect() as db:wid=db.execute('SELECT id FROM agent_workers LIMIT 1').fetchone()[0]
    received=[]
    usage={'input_tokens':1000,'input_tokens_details':{'cached_tokens':800},'output_tokens':40,
           'output_tokens_details':{'reasoning_tokens':10},'cost':0,
           'opencode':{'billing_source':'go','allowance':.0123,'list_cost':.01,'private_key':'must-not-log'}}
    class Upstream(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            received.append((self.path,dict(self.headers),json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
            data={'id':'private-response-id','status':'completed','usage':usage}
            body=('data: '+json.dumps({'type':'response.completed','response':data})+'\n\n').encode() if stream else json.dumps(data).encode()
            self.send_response(200);self.send_header('Content-Type','text/event-stream' if stream else 'application/json');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
    server=ThreadingHTTPServer(('127.0.0.1',0),Upstream);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    relay=ProviderTelemetry(tmp_path,wid,'fixture-go-run',{'provider':'opencode_go','effort':'default'},'synthetic-go-key',
        upstream=f'http://127.0.0.1:{server.server_port}/go/openai/v1',session_id='saved-go-conversation')
    try:
        with httpx.Client(trust_env=False) as client:
            response=client.post(relay.url+'/responses',headers={'Authorization':'Bearer synthetic-go-key','session_id':'native-codex-session'},
                json={'model':'kimi-k2.7-code','input':'private prompt','reasoning':{'effort':'high'}})
        assert response.status_code==200
        path,headers,body=received[0];headers={k.lower():v for k,v in headers.items()}
        assert path=='/go/openai/v1/responses' and headers['session_id']=='native-codex-session'
        assert headers['x-opencode-session']=='native-codex-session' and headers['user-agent']=='DDoJA-Codex/1.0'
        assert 'reasoning' not in body
        for _ in range(100):
            with s.connect() as db:row=db.execute('SELECT * FROM agent_provider_requests').fetchone()
            if row and row['input_tokens'] is not None:break
            time.sleep(.01)
        assert row['input_tokens']==1000 and row['cached_input_tokens']==800
        metadata=json.loads(row['metadata']);assert metadata['opencode']['allowance']==.0123
        for secret in ['private prompt','synthetic-go-key','native-codex-session','must-not-log']:assert secret not in row['metadata']
        with s.connect() as db:report=diagnostics(db)
        assert report['opencode_allowance_usd']==.0123 and report['reported_cost_usd']==0
        assert report['peak_cost_estimate_usd'] is None
    finally:relay.close();server.shutdown();server.server_close();thread.join(1)


def test_billing_exhaustion_pauses_but_transient_rate_limits_do_not():
    assert agent_runtime._is_quota_error({'message':'unexpected status 402 Payment Required: Insufficient Balance'})
    assert agent_runtime._is_quota_error({'code':'go_usage_limit_exceeded'})
    assert not agent_runtime._is_quota_error({'message':'HTTP 429: Too many requests; retry shortly'})
    secret='oc_'+'sk_'+'abcdefghijklmno0123456789'
    assert secret not in agent_runtime._redacted_stderr_excerpt(['OPENCODE_API_KEY='+secret],None)

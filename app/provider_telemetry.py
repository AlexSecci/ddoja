"""Loopback Responses relay: canonical JSON, streaming usage, hash-only traces.

No prompts, response bodies, token values or Authorization headers are logged.
The only production upstream is the user's selected official DeepSeek endpoint.
"""
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time
import uuid
import httpx
from .request_metrics import RequestMetrics


def canonical(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'))
def fingerprint(value):return hashlib.sha256(canonical(value).encode()).hexdigest()


def request_shape(body, previous=()):
    items=body.get('input',[])
    if not isinstance(items,list):items=[items]
    hashes=[fingerprint(i) for i in items]
    common=0
    for old,new in zip(previous,hashes):
        if old!=new:break
        common+=1
    shape={'configuration_sha256':fingerprint({k:v for k,v in body.items() if k not in {'input','metadata'}}),
           'instructions_sha256':fingerprint(body.get('instructions')),'tools_sha256':fingerprint(body.get('tools',[])),
           'input_sha256':fingerprint(items),'input_items':len(items),'input_utf8_bytes':len(canonical(items).encode()),
           'common_prefix_items_with_previous':common,
           'prefixes':{str(n):fingerprint(hashes[:n]) for n in (1,2,4,8,16,32,64,128,256) if n<=len(hashes)}}
    return shape,hashes


class ProviderTelemetry:
    def __init__(self, root, worker_id, run_id, execution, key, *, upstream='https://api.deepseek.com'):
        self.metrics=RequestMetrics(root);self.worker_id=worker_id;self.run_id=run_id;self.execution=execution
        self.key=key;self.upstream=upstream;self.nonce=uuid.uuid4().hex;self.previous=[];self.lock=threading.Lock()
        owner=self
        class Handler(BaseHTTPRequestHandler):
            protocol_version='HTTP/1.1'
            def log_message(self,*args):pass
            def do_POST(self):
                self.connection.settimeout(10)
                if (self.path != '/'+owner.nonce+'/responses' or
                        not hmac.compare_digest(self.headers.get('Authorization',''), 'Bearer '+owner.key)):
                    length=self.headers.get('Content-Length','0')
                    if length.isdigit() and int(length)<=65536:self.rfile.read(int(length))
                    self.send_error(403);return
                request_id=uuid.uuid4().hex;started=time.time();metadata={};usage=None;state='interrupted'
                try:
                    length=int(self.headers.get('Content-Length','0'))
                    if not 0<length<=64*1024*1024:raise ValueError('Invalid provider request size.')
                    body=json.loads(self.rfile.read(length))
                    if isinstance(body.get('tools'),list):body['tools']=sorted(body['tools'],key=lambda t:(t.get('type',''),t.get('name',t.get('function',{}).get('name',''))))
                    with owner.lock:
                        metadata,owner.previous=request_shape(body,owner.previous)
                    owner.record(request_id,started,'started',metadata)
                    with httpx.Client(timeout=httpx.Timeout(180),trust_env=False,follow_redirects=False) as client:
                        with client.stream('POST',owner.upstream+'/responses',content=canonical(body).encode(),
                                           headers={'Authorization':'Bearer '+owner.key,'Content-Type':'application/json','Accept-Encoding':'identity'}) as response:
                            metadata['response_headers_seconds']=round(time.time()-started,4)
                            self.send_response(response.status_code)
                            self.send_header('Content-Type',response.headers.get('Content-Type','application/json'))
                            self.send_header('Connection','close');self.end_headers();self.close_connection=True
                            buffer=b'';is_stream='text/event-stream' in response.headers.get('Content-Type','')
                            for chunk in response.iter_bytes():
                                buffer+=chunk
                                if is_stream:
                                    while b'\n\n' in buffer or b'\r\n\r\n' in buffer:
                                        marker=b'\r\n\r\n' if b'\r\n\r\n' in buffer else b'\n\n'
                                        frame,buffer=buffer.split(marker,1)
                                        data=b'\n'.join(line[5:].strip() for line in frame.splitlines() if line.startswith(b'data:'))
                                        try:event=json.loads(data)
                                        except (ValueError,UnicodeError):continue
                                        if event.get('type') in {'response.completed','response.incomplete','response.failed'}:
                                            value=event.get('response') or {};usage=value.get('usage');state=value.get('status',event['type'].split('.')[-1])
                                            metadata['provider_response_id_sha256']=fingerprint(value.get('id'))
                                            owner.record(request_id,started,state,metadata,usage)
                                if is_stream:self.wfile.write(chunk);self.wfile.flush()
                                if len(buffer)>8*1024*1024:buffer=b''
                            if not is_stream:
                                try:value=json.loads(buffer);usage=value.get('usage');state=value.get('status','completed' if response.is_success else 'failed')
                                except (ValueError,UnicodeError):state='failed'
                                owner.record(request_id,started,state,metadata,usage)
                                self.wfile.write(buffer);self.wfile.flush()
                            if response.status_code>=400:metadata['http_status']=response.status_code;state='failed'
                except (BrokenPipeError,ConnectionResetError):state='client_disconnected'
                except Exception as error:
                    metadata['error_type']=type(error).__name__;state='failed'
                    try:self.send_error(502,'Provider relay failed')
                    except OSError:pass
                finally:
                    metadata['upstream_and_transport_seconds']=round(time.time()-started,4)
                    owner.record(request_id,started,state,metadata,usage)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.server.daemon_threads=True;self.server.block_on_close=False
        self.thread=threading.Thread(target=lambda:self.server.serve_forever(poll_interval=.05),daemon=True);self.thread.start()
        self.url='http://127.0.0.1:'+str(self.server.server_port)+'/'+self.nonce

    def record(self,*args):
        request_id,started,state,metadata,*usage=args
        self.metrics.record(request_id,self.worker_id,self.run_id,self.execution,started,state,metadata,usage[0] if usage else None)

    def close(self):
        self.server.shutdown();self.server.server_close();self.thread.join(timeout=2)

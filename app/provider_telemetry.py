"""Loopback Responses relay: canonical JSON, streaming usage, hash-only traces.

No prompts, response bodies, token values or Authorization headers are logged.
The only production upstream is the user's selected official DeepSeek endpoint.
"""
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
import time
import uuid
import httpx
from .request_metrics import RequestMetrics


def canonical(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'))
def fingerprint(value):return hashlib.sha256(canonical(value).encode()).hexdigest()


def native_worker_tools(root, worker):
    """Expose the same registered MCP schemas directly, avoiding native tool_search.

    DeepSeek's native tool-search calls can arrive as literal DSML text. The
    CLI already registers these MCP handlers, so a normal namespaced function
    call can invoke them without a separate deferred-schema lookup.
    """
    context=worker.get('context') or {}
    if isinstance(context,str):context=json.loads(context)
    contract_id=context.get('instruction_contract_id')
    if contract_id:
        from .worker_contracts import load
        bridge=load(root,worker['id'],contract_id)['agent_tools']
    else:
        from tools.agent_mcp import ROLE_TOOLS,TOOL_DEFINITIONS
        bridge=[{'name':n,**TOOL_DEFINITIONS[n]} for n in ROLE_TOOLS[worker['role']]]

    def namespace(name,definitions):
        return {'type':'namespace','name':name,'description':'Tools in the '+name+' namespace.',
                'tools':[{'type':'function','name':t['name'],'description':t.get('description',''),
                          'parameters':t['inputSchema'],'strict':False} for t in definitions]}

    catalog=[namespace('mcp__agent_bridge',bridge)]
    backend=(context.get('execution') or {}).get('browser_backend','none')
    if backend=='playwright':
        if not contract_id:raise ValueError('A pinned browser catalog is required for direct Playwright tools.')
        path=Path(root)/'data/worker-contracts'/worker['id']/(contract_id+'.browser.json')
        # The required MCP browser finishes initialization before the CLI's
        # first inference request and saves its exact advertised schemas here.
        saved=json.loads(path.read_text(encoding='utf-8'))
        from .browser_backends import PLAYWRIGHT_TOOLS
        if saved.get('sha256')!=fingerprint(saved['tools']) or not {t['name'] for t in saved['tools']}<=set(PLAYWRIGHT_TOOLS):
            raise ValueError('Pinned browser schemas failed integrity or scope validation.')
        catalog.append(namespace('mcp__playwright',saved['tools']))
    return catalog


def scoped_tools(tools, role, backend):
    """Advertise only the worker's existing bridge/browser allowlist.

    CLI host tools are useful for coding, but are not workflow capabilities.
    Filtering schemas prevents misleading probes; runtime/Store checks remain.
    """
    from tools.agent_mcp import ROLE_TOOLS
    from .browser_backends import PLAYWRIGHT_TOOLS
    allowed={'mcp__agent_bridge':set(ROLE_TOOLS[role])}
    if role in {'discovery','evaluator','submission'}:
        if backend=='playwright':allowed['mcp__playwright']=set(PLAYWRIGHT_TOOLS)
        elif backend=='cua':allowed['mcp__cua_repl']={'js'}

    def keep(tool, namespace=None):
        if not isinstance(tool,dict):return None
        # Current CLI versions defer MCP schemas behind client-side tool_search.
        # Their loaded tools arrive in tool_search_output input items, rather
        # than the initial top-level tools list. Keep this registry lookup;
        # only scoped MCP servers are configured by the worker launch.
        if tool.get('type')=='tool_search' and tool.get('execution')=='client':return tool
        name=tool.get('name','')
        if tool.get('type')=='namespace':
            children=[value for child in tool.get('tools',[]) if (value:=keep(child,name)) is not None]
            return {**tool,'tools':children} if children else None
        if tool.get('type')!='function':return None
        scope=tool.get('namespace',namespace)
        if scope in allowed and name in allowed[scope]:return tool
        # Older CLI catalogs flatten MCP-qualified names instead of namespaces.
        for prefix,names in allowed.items():
            if name in {prefix+'__'+n for n in names}:return tool
        return None

    return [value for tool in tools if (value:=keep(tool)) is not None]


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
    def __init__(self, root, worker_id, run_id, execution, key, *, upstream='https://api.deepseek.com', role=None, tool_catalog=None):
        self.metrics=RequestMetrics(root);self.worker_id=worker_id;self.run_id=run_id;self.execution=execution
        self.role=role
        self.catalog_source=tool_catalog;self.catalog=None
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
                    offered=len(body.get('tools',[]))
                    if isinstance(body.get('tools'),list):
                        if owner.role is not None:body['tools']=scoped_tools(body['tools'],owner.role,owner.execution.get('browser_backend','none'))
                        if owner.catalog_source is not None:
                            with owner.lock:
                                if owner.catalog is None:owner.catalog=owner.catalog_source()
                            body['tools']=[t for t in body['tools'] if t.get('type')!='tool_search']
                            groups={(t.get('type'),t.get('name')):t for t in body['tools']}
                            groups.update({(t.get('type'),t.get('name')):t for t in owner.catalog})
                            body['tools']=list(groups.values())
                        if owner.role is not None and offered and not body['tools']:
                            raise ValueError('The CLI tool catalog has no supported scoped tools or client-side MCP lookup.')
                        body['tools']=sorted(body['tools'],key=lambda t:(t.get('type',''),t.get('name',t.get('function',{}).get('name',''))))
                    with owner.lock:
                        metadata,owner.previous=request_shape(body,owner.previous)
                    if owner.role is not None:
                        metadata.update(offered_tool_groups=offered,scoped_tool_groups=len(body.get('tools',[])))
                    if owner.catalog_source is not None:metadata['direct_mcp_schemas']=True
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

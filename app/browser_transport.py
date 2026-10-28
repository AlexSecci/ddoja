"""Small synchronous Streamable HTTP MCP client for the owned browser service."""
import json
import httpx


class BrowserRPC:
    def __init__(self,endpoint):
        self.client=httpx.Client(timeout=90,trust_env=False)
        self.endpoint=endpoint;self.session=None;self.sequence=0
        self.request('initialize',{'protocolVersion':'2024-11-05','capabilities':{},'clientInfo':{'name':'ddoja-owned-browser','version':'1'}})
        self.request('notifications/initialized',{},notification=True)

    def request(self,method,params,notification=False):
        self.sequence+=1
        body={'jsonrpc':'2.0','method':method,'params':params}
        if not notification:body['id']=self.sequence
        headers={'Accept':'application/json, text/event-stream','MCP-Protocol-Version':'2024-11-05'}
        if self.session:headers['Mcp-Session-Id']=self.session
        response=self.client.post(self.endpoint,json=body,headers=headers)
        response.raise_for_status()
        self.session=response.headers.get('mcp-session-id',self.session)
        if notification or response.status_code==202:return {}
        messages=[json.loads(line[5:].strip()) for line in response.text.splitlines() if line.startswith('data:')]
        if not messages:messages=[response.json()]
        message=next((m for m in messages if m.get('id')==body['id']),None)
        if not message:raise RuntimeError('Browser service returned no matching MCP response.')
        if 'error' in message:raise RuntimeError(message['error'].get('message','Browser MCP error'))
        return message['result']

    def tools(self):return self.request('tools/list',{})['tools']
    def call(self,name,args):return self.request('tools/call',{'name':name,'arguments':args})
    def close(self):self.client.close()  # Never delete the shared worker context.
    def __enter__(self):return self
    def __exit__(self,*exc):self.close()

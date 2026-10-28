"""Scoped stdio adapter to a dashboard-owned persistent Playwright service."""
import argparse
import json
from pathlib import Path
import sys
import copy
import re

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.browser_backends import PLAYWRIGHT_TOOLS
from app.browser_sessions import BrowserSessions
from app.browser_transport import BrowserRPC
from app.efficiency_features import features


class BrowserBridge:
    def __init__(self,root,worker_id,role,contract_id=None):
        self.root=Path(root);self.worker_id=worker_id;self.role=role
        self.settings=features(root)
        if contract_id:
            from app.worker_contracts import load
            self.settings = load(root,worker_id,contract_id)['features']
        session=BrowserSessions(root).inspect(worker_id)
        if session['state']!='live':raise RuntimeError('The owned browser service is unavailable. Request host recovery; do not start a replacement from this CLI.')
        self.rpc=BrowserRPC(session['endpoint'])
        self.catalog=sorted([t for t in self.rpc.tools() if t['name'] in PLAYWRIGHT_TOOLS],key=lambda t:t['name'])
        if self.settings['batch_form_edits'] and role=='submission':
            self.catalog=copy.deepcopy(self.catalog)
            for tool in self.catalog:
                if tool['name']=='browser_fill_form':
                    tool['description']='Fill a group of already-mapped ordinary form fields in one call and return a fresh inline snapshot. Do not batch signatures, binding acceptance, CAPTCHA, file uploads or final sends. On partial failure inspect the returned state before retrying.'
                    tool['inputSchema']['properties'].update(
                        expected_url={'type':'string','description':'Optional exact URL from the current observation; mismatch stops before editing.'},
                        observe_target={'type':'string','description':'Optional observed form/section target for the returned snapshot; omit for the complete page.'})
        if contract_id:
            path=self.root/'data/worker-contracts'/worker_id/(contract_id+'.browser.json')
            from app.worker_contracts import encoded
            import hashlib
            if path.exists():
                saved=json.loads(path.read_text(encoding='utf-8'))
                catalog=saved['tools']
                if saved['sha256'] != hashlib.sha256(encoded(catalog).encode()).hexdigest():
                    raise ValueError('Pinned browser catalog changed on disk.')
                if not {t['name'] for t in catalog} <= set(PLAYWRIGHT_TOOLS):
                    raise ValueError('Pinned browser catalog contains an unsupported tool.')
                self.catalog=catalog
            else:
                saved={'tools':self.catalog,'sha256':hashlib.sha256(encoded(self.catalog).encode()).hexdigest()}
                temp=path.with_suffix('.tmp');temp.write_text(encoded(saved),encoding='utf-8');temp.replace(path)

    def tools(self):return self.catalog

    def call(self,name,arguments):
        if name not in {t['name'] for t in self.catalog}:raise ValueError('Browser tool is outside this worker allowlist.')
        if name=='browser_fill_form' and self.settings['batch_form_edits'] and self.role=='submission':
            arguments=dict(arguments);target=arguments.pop('observe_target',None);expected=arguments.pop('expected_url',None)
            fields=arguments.get('fields')
            if not isinstance(fields,list) or not 1<=len(fields)<=40:raise ValueError('Fill between 1 and 40 observed fields per batch.')
            # Binding decisions are kept out of the batch convenience path;
            # ordinary checkbox/radio operations remain separate observed UI actions.
            if any(f.get('type') not in {'textbox','combobox','slider'} for f in fields):
                raise ValueError('Batch only ordinary text/select/slider fields. Consent and choice controls need their separate observed action.')
            if expected:
                current=self.rpc.call('browser_snapshot',{'depth':1})
                text='\n'.join(b.get('text','') for b in current.get('content',[]))
                url=re.search(r'(?m)^- Page URL: (\S+)',text)
                if not url or url.group(1)!=expected:
                    return {'isError':True,'content':[{'type':'text','text':'The observed page URL changed. No batch fields were edited.'},*current.get('content',[])]}
            result=self.rpc.call(name,arguments)
            snapshot=self.rpc.call('browser_snapshot',{'target':target} if target else {})
            failed=bool(result.get('isError') or snapshot.get('isError'))
            note=('A field batch failed and earlier fields may have changed. Inspect the current snapshot; no automatic retry was made.' if result.get('isError') else f'Applied {len(fields)} observed ordinary fields in one batch. The following snapshot is the current browser state.')
            return {'isError':failed,'content':[{'type':'text','text':note},
                    *(result.get('content',[]) if result.get('isError') else []),*snapshot.get('content',[])],
                    '_meta':{'ddoja':{'batch_fields':len(fields),'snapshot_included':True,'automatic_retry':False}}}
        return self.rpc.call(name,arguments)


def serve(bridge):
    for raw in sys.stdin.buffer:
        try:
            request=json.loads(raw);identifier=request.get('id')
            if identifier is None:continue
            method=request.get('method');params=request.get('params',{})
            if method=='initialize':
                result={'protocolVersion':'2024-11-05','capabilities':{'tools':{}},'serverInfo':{'name':'ddoja-persistent-browser','version':'1'},
                        'instructions':'This context belongs only to the current worker and survives CLI disconnects. Inspect existing tabs before navigating; preserve unsaved forms. Final sends and binding/CAPTCHA approvals remain separate scoped actions.'}
            elif method=='tools/list':result={'tools':bridge.tools()}
            elif method=='ping':result={}
            elif method=='tools/call':result=bridge.call(params.get('name'),params.get('arguments',{}))
            else:raise ValueError('Unknown MCP method.')
            response={'jsonrpc':'2.0','id':identifier,'result':result}
        except Exception as error:
            response={'jsonrpc':'2.0','id':request.get('id'),'error':{'code':-32602,'message':str(error)}}
        sys.stdout.buffer.write((json.dumps(response,separators=(',',':'))+'\n').encode('utf-8'));sys.stdout.buffer.flush()


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--root',required=True);parser.add_argument('--worker',required=True);parser.add_argument('--role',required=True);parser.add_argument('--contract')
    args=parser.parse_args();bridge=BrowserBridge(args.root,args.worker,args.role,args.contract)
    try:serve(bridge)
    finally:bridge.rpc.close()

"""Optional event-driven, read-only Gmail broker. No model or mailbox-wide scan."""
import base64
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import threading
import time
from urllib.parse import urlsplit
import httpx
from email.utils import getaddresses, parseaddr
from .provider_credentials import _dpapi


class _Text(HTMLParser):
    def __init__(self):super().__init__();self.parts=[]
    def handle_data(self,data):self.parts.append(data)


def extract_code(payload):
    chunks=[]
    def walk(part):
        data=part.get('body',{}).get('data')
        if data and part.get('mimeType') in {'text/plain','text/html'}:
            text=base64.urlsafe_b64decode(data+'='*(-len(data)%4)).decode('utf-8',errors='replace')
            parser=_Text();parser.feed(text);chunks.append(' '.join(parser.parts))
        for child in part.get('parts',[]):walk(child)
    walk(payload);text=' '.join(chunks)
    match=re.search(r'(?:(?:security|verification)\s+code|application)\s*(?:is|:)\s*([A-Za-z0-9]{8})\b',text,re.I)
    if match:return match.group(1)
    candidates={v for v in re.findall(r'\b[A-Za-z0-9]{8}\b',text) if any(c.isdigit() for c in v) and any(c.isalpha() for c in v)}
    return next(iter(candidates)) if len(candidates)==1 else None


class AuthBroker:
    def __init__(self,store,on_ready):
        self.store=store;self.on_ready=on_ready;self.pool=ThreadPoolExecutor(max_workers=2,thread_name_prefix='scoped-mail')
        self.active=set();self.lock=threading.Lock()

    @property
    def credential_path(self):return self.store.root/'data/secrets/gmail-readonly.dpapi'

    def status(self):return {'configured':self.credential_path.is_file(),'mode':'gmail_readonly_oauth' if self.credential_path.is_file() else 'codex_connector_fallback'}

    def wake(self,worker_id,request_id):
        if not self.credential_path.is_file():return self.status()
        with self.lock:
            if (worker_id,request_id) in self.active:return {'status':'already_servicing'}
            self.active.add((worker_id,request_id))
        self.pool.submit(self._service,worker_id,request_id)
        return {'status':'servicing'}

    def _service(self,worker_id,request_id):
        try:
            from .ephemeral_auth import authorization
            grant=authorization(self.store.root,worker_id)
            request_path=self.store.root/'data/auth-handoffs'/(worker_id+'.request.json')
            request=json.loads(request_path.read_text());worker=self.store.get_agent_worker(worker_id)
            job=self.store.get_job(worker['job_id'])
            if not grant or request['request_id']!=request_id or request.get('cancelled_at_unix'):return
            if urlsplit(job['url']).hostname!='job-boards.greenhouse.io':return
            credentials=json.loads(_dpapi(self.credential_path.read_bytes(),decrypt=True))
            with httpx.Client(timeout=15,trust_env=False) as client:
                response=client.post('https://oauth2.googleapis.com/token',data={**{k:credentials[k] for k in ('client_id','client_secret','refresh_token')},'grant_type':'refresh_token'})
                response.raise_for_status();headers={'Authorization':'Bearer '+response.json()['access_token']}
                query='from:no-reply@us.greenhouse-mail.io subject:"Security code for your application" after:'+str(int(request['requested_at_unix'])-30)
                config=self.store.root/'config/submission-efficiency.json'
                seconds=json.loads(config.read_text()).get('broker_lookup_seconds',20) if config.exists() else 20
                deadline=time.monotonic()+min(60,max(1,seconds))
                while time.monotonic()<deadline:
                    # A changed task, nonce or auth owner cancels this lookup.
                    current=json.loads(request_path.read_text());live=self.store.get_agent_worker(worker_id)
                    if current['request_id']!=request_id or live['state'] not in {'running','waiting_browser'}:return
                    with self.store.connect() as db:
                        gate=db.execute("SELECT worker_id FROM agent_browser_controls WHERE scope_key=? AND state='open'",('company:'+job['company_id'],)).fetchone()
                    if gate and gate[0]!=worker_id:return
                    listing=client.get('https://gmail.googleapis.com/gmail/v1/users/me/messages',params={'q':query,'maxResults':3},headers=headers);listing.raise_for_status()
                    candidates=listing.json().get('messages',[])
                    if len(candidates)>1:
                        from .auth_handoffs import outcome
                        outcome(self.store,worker_id,request_id,'ambiguous_mail');self.on_ready();return
                    if candidates:
                        message=client.get('https://gmail.googleapis.com/gmail/v1/users/me/messages/'+candidates[0]['id'],params={'format':'full'},headers=headers);message.raise_for_status();mail=message.json()
                        fields={h['name'].lower():h['value'] for h in mail['payload'].get('headers',[])}
                        recipients={address.lower() for _,address in getaddresses([fields.get('to','')])}
                        if (credentials['email'].lower() not in recipients
                                or parseaddr(fields.get('from',''))[1].lower()!='no-reply@us.greenhouse-mail.io'
                                or int(mail['internalDate'])/1000<request['requested_at_unix']-30):
                            raise ValueError('Scoped message identity did not match.')
                        code=extract_code(mail['payload'])
                        if code:
                            from .auth_handoffs import deliver
                            deliver(self.store,worker_id,candidates[0]['id'],code);self.on_ready();return
                    time.sleep(1)
                from .auth_handoffs import outcome
                outcome(self.store,worker_id,request_id,'no_fresh_mail');self.on_ready()
        except Exception as error:
            with self.store.connect(write=True) as db:
                self.store.event(db,'agent.auth_broker_attention',{'worker_id':worker_id,'request_id':request_id,'error_type':type(error).__name__},actor='auth_broker')
            try:
                from .auth_handoffs import outcome
                outcome(self.store,worker_id,request_id,'broker_unavailable');self.on_ready()
            except (ValueError,OSError,KeyError):pass
        finally:
            with self.lock:self.active.discard((worker_id,request_id))

    def close(self):self.pool.shutdown(wait=False,cancel_futures=True)

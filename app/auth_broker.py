"""Optional event-driven, read-only Gmail broker. No model or mailbox-wide scan."""
import base64
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import threading
import time
from datetime import datetime
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


def verification_mail_window(store, worker, job):
    """Use the observed send/challenge transition, not a delayed model checkpoint.

    Only a narrow, recent, exact-page transition is usable. A resend or another
    Submit invalidates the older window. Ambiguity still fails closed below.
    """
    click = None
    window = None
    context=worker.get('context') or {}
    if isinstance(context,str):context=json.loads(context)
    authorized_at=datetime.fromisoformat(context['final_action_authorized_at']).timestamp() if context.get('final_action_authorized_at') else None
    with store.connect() as db:
        rows = db.execute("SELECT tool_name,payload,created_at FROM agent_runtime_events "
            "WHERE worker_id=? AND success=1 AND tool_name IN "
            "('playwright.browser_click','playwright.browser_snapshot') ORDER BY rowid",
            (worker['id'],)).fetchall()
        for row in rows:
            value = json.loads(row['payload'])
            item = value.get('transport_event', {}).get('item', {})
            args = item.get('arguments') or {}
            when = datetime.fromisoformat(row['created_at']).timestamp()
            url = value.get('observed_application_url') or store._agent_observed_application_url(db, worker, value)
            proof = value.get('verification_observation') or {}
            if row['tool_name'] == 'playwright.browser_click':
                label = str(args.get('element') or '').casefold()
                if 'resend' in label or 'send a new code' in label:
                    click = window = None
                elif 'submit' in label:
                    window = None
                    click = when if url == job['url'] else None
                elif (not label and url==job['url'] and authorized_at is not None
                        and 0<=when-authorized_at<=30 and proof.get('present')
                        and proof.get('filled_boxes')==0 and proof.get('submit_enabled') is False):
                    # Secret redaction removes click arguments after the page
                    # starts showing a code. The host final-action timestamp
                    # plus the returned empty challenge retains the transition.
                    window=None;click=when
                else:
                    click=window=None
            if (click is not None and window is None and url == job['url']
                    and proof.get('present') and proof.get('filled_boxes') == 0
                    and proof.get('submit_enabled') is False and 0 <= when-click <= 20):
                window = (int(click)-2, int(when)+3)
    if window and 0 <= time.time()-window[1] <= 900:
        return window
    return None


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
                window=verification_mail_window(self.store,worker,job)
                after,before=window or (int(request['requested_at_unix'])-30,None)
                query='from:no-reply@us.greenhouse-mail.io subject:"Security code for your application" after:'+str(after)
                if before is not None:query+=' before:'+str(before)
                config=self.store.root/'config/submission-efficiency.json'
                seconds=json.loads(config.read_text()).get('broker_lookup_seconds',20) if config.exists() else 20
                deadline=time.monotonic()+min(60,max(1,seconds))
                while time.monotonic()<deadline:
                    # A changed task, nonce or auth owner cancels this lookup.
                    current=json.loads(request_path.read_text());live=self.store.get_agent_worker(worker_id)
                    if (current['request_id']!=request_id or current.get('cancelled_at_unix')
                            or live['state'] not in {'running','waiting_browser'}):return
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
                                or int(mail['internalDate'])/1000<after
                                or before is not None and int(mail['internalDate'])/1000>=before):
                            raise ValueError('Scoped message identity did not match.')
                        with self.store.connect() as db:
                            claimed=db.execute("SELECT 1 FROM events WHERE kind='agent.email_token_delivered' "
                                "AND json_extract(payload,'$.message_id')=? "
                                "AND json_extract(payload,'$.worker_id')!=?",(candidates[0]['id'],worker_id)).fetchone()
                        if claimed:raise ValueError('This email was already bound to a different task.')
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

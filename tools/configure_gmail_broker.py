"""Authorize the dashboard's read-only Gmail broker using a desktop OAuth client."""
import argparse
import base64
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import secrets
import sys
import time
from urllib.parse import parse_qs, urlencode, urlsplit
import webbrowser
import httpx

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from app.provider_credentials import _dpapi

SCOPE='https://www.googleapis.com/auth/gmail.readonly'


def configure(client_file, root=ROOT, expected_email='applicant@example.invalid'):
    client=json.loads(Path(client_file).read_text(encoding='utf-8')).get('installed')
    if not client or not client.get('client_id') or not client.get('client_secret'):
        raise ValueError('Provide a Google Desktop app client JSON file.')
    state=secrets.token_urlsafe(32);verifier=secrets.token_urlsafe(64);result={}
    class Callback(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_GET(self):
            query=parse_qs(urlsplit(self.path).query)
            if urlsplit(self.path).path!='/callback' or not secrets.compare_digest(query.get('state',[''])[0],state):
                self.send_error(400,'Invalid OAuth callback');return
            result.update(code=query.get('code',[None])[0],error=bool(query.get('error')))
            self.send_response(200);self.send_header('Content-Type','text/plain');self.end_headers()
            self.wfile.write(b'Google sign-in returned. You can close this tab and check the local setup command.')
    with HTTPServer(('127.0.0.1',0),Callback) as server:
        server.timeout=1;redirect=f'http://127.0.0.1:{server.server_port}/callback'
        url='https://accounts.google.com/o/oauth2/v2/auth?'+urlencode({
            'client_id':client['client_id'],'redirect_uri':redirect,'response_type':'code',
            'scope':SCOPE,'access_type':'offline','prompt':'consent','state':state,
            'login_hint':expected_email,'code_challenge_method':'S256',
            'code_challenge':base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()})
        print('Opening Google sign-in. Choose the expected Gmail account and review read-only access.',flush=True)
        if not webbrowser.open(url):raise RuntimeError('The system browser did not open Google sign-in.')
        deadline=time.monotonic()+900
        while not result and time.monotonic()<deadline:server.handle_request()
    if not result.get('code') or result.get('error'):raise ValueError('Google authorization was cancelled or timed out.')
    with httpx.Client(timeout=20,trust_env=False) as http:
        reply=http.post('https://oauth2.googleapis.com/token',data={
            'client_id':client['client_id'],'client_secret':client['client_secret'],
            'code':result['code'],'code_verifier':verifier,'redirect_uri':redirect,'grant_type':'authorization_code'})
        if not reply.is_success:raise ValueError('Google could not exchange the authorization code. No credentials were saved.')
        tokens=reply.json()
        if tokens.get('scope') and set(tokens['scope'].split())!={SCOPE}:
            raise ValueError('Google returned a different access scope. No credentials were saved.')
        if not tokens.get('refresh_token'):raise ValueError('Google did not return offline access. No credentials were saved.')
        profile=http.get('https://gmail.googleapis.com/gmail/v1/users/me/profile',headers={'Authorization':'Bearer '+tokens['access_token']})
        if not profile.is_success or profile.json().get('emailAddress','').lower()!=expected_email.lower():
            raise ValueError('The authorized mailbox does not match the configured applicant email. No credentials were saved.')
    path=Path(root)/'data/secrets/gmail-readonly.dpapi';path.parent.mkdir(parents=True,exist_ok=True)
    value={k:client[k] for k in ('client_id','client_secret')}
    value.update(refresh_token=tokens['refresh_token'],email=expected_email,scope=SCOPE)
    temporary=path.with_suffix('.tmp');temporary.write_bytes(_dpapi(json.dumps(value).encode()));temporary.replace(path)
    return {'configured':True,'scope':SCOPE,'storage':'Windows user-bound encrypted storage'}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--client-file',required=True)
    args=parser.parse_args()
    try:print(json.dumps(configure(args.client_file)))
    except Exception as error:print(str(error) if isinstance(error,(ValueError,RuntimeError)) else 'Google setup failed: '+type(error).__name__);sys.exit(1)

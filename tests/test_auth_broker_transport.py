import base64
import json
import time

import httpx
import pytest

from app import auth_broker, auth_handoffs, ephemeral_auth
from test_email_handoff import fixture


def broker_fixture(tmp_path, monkeypatch, mode):
    store, pipeline, job, worker_id = fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(auth_broker, '_dpapi', lambda value, decrypt=False: value[::-1])
    monkeypatch.setattr(auth_handoffs, '_dpapi', lambda value, decrypt=False: value[::-1])
    broker = auth_broker.AuthBroker(store, lambda: None)
    broker.credential_path.parent.mkdir(parents=True, exist_ok=True)
    broker.credential_path.write_bytes(json.dumps({
        'client_id': 'fictional-client', 'client_secret': 'fictional-secret',
        'refresh_token': 'fictional-refresh', 'email': 'applicant@example.invalid'}).encode()[::-1])
    folder = store.root / 'data/auth-handoffs'; folder.mkdir(exist_ok=True)
    request = {**ephemeral_auth.authorization(store.root, worker_id),
               'request_id': 'same-scoped-request', 'requested_at_unix': time.time()}
    path = folder / (worker_id + '.request.json'); path.write_text(json.dumps(request))
    with store.connect(write=True) as db:
        db.execute("UPDATE agent_workers SET checkpoint='awaiting_email_token_handoff' WHERE id=?", (worker_id,))
    text = '### Page\n- Page URL: ' + job['url'] + '\n### Snapshot\n- group "enter the 8-character code to confirm":\n'
    text += '\n'.join('- textbox [ref=code%d]' % i for i in range(8))
    text += '\n- button "Submit application" [disabled] [ref=submit]'
    store.agent_runtime_event(worker_id, {'type': 'tool_result', 'tool_name': 'playwright.browser_snapshot',
        'success': True, 'session_id': 'fixture-session', 'transport_event': {'type': 'item.completed', 'item': {
        'type': 'mcp_tool_call', 'server': 'playwright', 'tool': 'browser_snapshot', 'status': 'completed',
        'result': {'content': [{'type': 'text', 'text': text}]}}}})
    calls = []
    real_client = httpx.Client

    def handler(req):
        stage = ('oauth_refresh' if req.url.host == 'oauth2.googleapis.com' else
                 'message_search' if req.url.path.endswith('/messages') else 'message_read')
        calls.append(stage)
        if mode in {'nonce_changed', 'user_stopped'} and len(calls) == 1:
            if mode == 'nonce_changed':
                changed = json.loads(path.read_text()); changed['request_id'] = 'different-request'
                path.write_text(json.dumps(changed))
            else:
                with store.connect(write=True) as db:
                    db.execute('UPDATE pipelines SET stop_requested=1 WHERE id=?', (pipeline['id'],))
            raise httpx.ConnectError('Fictional connection failure', request=req)
        if mode == 'connect_exhausted' or mode == stage and calls.count(stage) == 1:
            raise httpx.ConnectError('Fictional connection failure', request=req)
        if mode == 'invalid_credentials':
            return httpx.Response(401, json={'error': 'invalid_grant'})
        if mode == 'rate_limited' and stage == 'message_search' and calls.count(stage) == 1:
            return httpx.Response(429, json={'error': 'synthetic rate limit'})
        if stage == 'oauth_refresh':
            return httpx.Response(200, json={'access_token': 'fictional-access'})
        if stage == 'message_search':
            assert 'from:no-reply@us.greenhouse-mail.io' in req.url.params['q']
            assert 'subject:' in req.url.params['q'] and 'after:' in req.url.params['q']
            return httpx.Response(200, json={'messages': [{'id': 'fictional-matching-message'}]})
        if mode == 'nonce_changed_after_read':
            changed = json.loads(path.read_text()); changed['request_id'] = 'different-request'
            path.write_text(json.dumps(changed))
        elif mode == 'user_stopped_after_read':
            with store.connect(write=True) as db:
                db.execute('UPDATE pipelines SET stop_requested=1 WHERE id=?', (pipeline['id'],))
        return httpx.Response(200, json={'internalDate': str(int(time.time() * 1000)), 'payload': {
            'mimeType': 'text/plain', 'headers': [{'name': 'To', 'value': 'applicant@example.invalid'},
                {'name': 'From', 'value': 'no-reply@us.greenhouse-mail.io'}],
            'body': {'data': base64.urlsafe_b64encode(b'Your security code is Ab1Cd2Ef').decode()}}})

    monkeypatch.setattr(auth_broker.httpx, 'Client', lambda **kwargs: real_client(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(auth_broker.time, 'sleep', lambda _: None)
    return broker, store, worker_id, request, calls


@pytest.mark.parametrize('mode', ['oauth_refresh', 'message_search', 'message_read', 'rate_limited'])
def test_transient_google_errors_retry_same_scope_and_deliver_encrypted_code(tmp_path, monkeypatch, mode):
    broker, store, wid, request, calls = broker_fixture(tmp_path, monkeypatch, mode)
    try:
        broker._service(wid, request['request_id'])
        assert (store.root / 'data/auth-handoffs' / (wid + '.dpapi')).exists()
        assert not (store.root / 'data/auth-handoffs' / (wid + '.outcome.json')).exists()
        with store.connect() as db:
            retries = db.execute("SELECT payload FROM events WHERE kind='agent.auth_broker_transport_retry'").fetchall()
            assert len(retries) == 1
            assert json.loads(retries[0][0])['request_id'] == request['request_id']
            assert db.execute("SELECT COUNT(*) FROM events WHERE kind='agent.email_token_delivered'").fetchone()[0] == 1
        assert 'Ab1Cd2Ef' not in store.path.read_bytes().decode('utf-8', errors='ignore')
        value = ephemeral_auth.consume(store.root, wid)
        assert value['code'] == 'Ab1Cd2Ef'
        consumed = json.loads((store.root / 'data/auth-handoffs' / (wid + '.consumed.json')).read_text())
        assert consumed['request_id'] == request['request_id']
    finally:
        broker.close()


@pytest.mark.parametrize('mode,attempts', [('connect_exhausted', 3), ('invalid_credentials', 1),
                                         ('nonce_changed', 1), ('user_stopped', 1)])
def test_retries_are_bounded_and_cancelled_requests_or_auth_errors_do_not_deliver(tmp_path, monkeypatch, mode, attempts):
    broker, store, wid, request, calls = broker_fixture(tmp_path, monkeypatch, mode)
    try:
        broker._service(wid, request['request_id'])
        assert len(calls) == attempts
        assert not (store.root / 'data/auth-handoffs' / (wid + '.dpapi')).exists()
        outcome = store.root / 'data/auth-handoffs' / (wid + '.outcome.json')
        assert outcome.exists() == (mode in {'connect_exhausted', 'invalid_credentials'})
        with store.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM events WHERE kind='agent.email_token_delivered'").fetchone()[0] == 0
    finally:
        broker.close()


@pytest.mark.parametrize('mode', ['nonce_changed_after_read', 'user_stopped_after_read'])
def test_successful_mail_reply_after_request_changes_cannot_deliver_code(tmp_path, monkeypatch, mode):
    broker, store, wid, request, calls = broker_fixture(tmp_path, monkeypatch, mode)
    try:
        broker._service(wid, request['request_id'])
        assert calls == ['oauth_refresh', 'message_search', 'message_read']
        assert not (store.root / 'data/auth-handoffs' / (wid + '.dpapi')).exists()
        with store.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM events WHERE kind='agent.email_token_delivered'").fetchone()[0] == 0
    finally:
        broker.close()

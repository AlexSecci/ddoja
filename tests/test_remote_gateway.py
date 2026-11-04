import json

import httpx
from fastapi.testclient import TestClient
import pytest
from argon2 import PasswordHasher

from app.remote_gateway import create_app
from app.dashboard_auth import DashboardAuth, COOKIE


class FixtureStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b'fictional response'


@pytest.fixture
def gateway(tmp_path):
    config = tmp_path / 'gateway.json'
    origin = 'https://fictional-private-dashboard.trycloudflare.com'
    host = 'ddoja-' + 'a' * 64 + '.invalid'
    config.write_text(json.dumps({'enabled': True, 'origin': origin, 'ingress_host': host,
                                 'authentication': 'password'}))
    seen = []

    def upstream(request):
        seen.append(request)
        return httpx.Response(200, stream=FixtureStream(), headers={'content-type': 'text/plain'})

    auth = DashboardAuth(tmp_path, hasher=PasswordHasher(time_cost=1, memory_cost=1024, parallelism=1))
    auth.configure('owner', 'synthetic long password for tests')
    with TestClient(create_app(config, transport=httpx.MockTransport(upstream), auth=auth),
                    base_url=origin, headers={'Host': host}) as client:
        assert client.post('/auth/login', headers={'Origin': origin},
                           json={'username': 'owner', 'password': 'synthetic long password for tests'}).status_code == 200
        yield client, config, origin, host, seen


def test_disabled_gateway_and_unknown_connector_cannot_obtain_session(gateway):
    client, config, origin, host, seen = gateway
    assert client.get('/api/session', headers={'Host': 'unknown.invalid'}).status_code == 403
    config.write_text('{}')
    assert client.get('/api/session', headers={'Host': host}).status_code == 503
    assert seen == []


@pytest.mark.parametrize('headers', [{}, {'Origin': 'https://attacker.example'},
                                   {'Origin': 'null'}, {'Origin': 'http://127.0.0.1:8765'}])
def test_cross_origin_and_missing_origin_actions_never_reach_dashboard(gateway, headers):
    client, config, origin, host, seen = gateway
    assert client.post('/api/pipelines/fictional/stop', headers={'Host': host, **headers}).status_code == 403
    assert seen == []


def test_valid_action_keeps_api_guard_and_fixed_destination(gateway):
    client, config, origin, host, seen = gateway
    r = client.put('/api/settings/workers?example=1', content=b'fictional', headers={
        'Host': host, 'Origin': origin, 'X-Pipeline-Token': 'synthetic-session-token',
        'Forwarded': 'host=attacker.example', 'X-Forwarded-Host': 'attacker.example',
        'Cookie': COOKIE + '=' + client.cookies.get(COOKIE) + '; private-auth-cookie=synthetic', 'Authorization': 'Bearer synthetic',
        'CF-Access-Jwt-Assertion': 'synthetic'})
    assert r.status_code == 200
    sent = seen[0]
    assert str(sent.url) == 'http://127.0.0.1:8765/api/settings/workers?example=1'
    assert sent.headers['origin'] == 'http://127.0.0.1:8765'
    assert sent.headers['x-pipeline-token'] == 'synthetic-session-token'
    assert not any(k in sent.headers for k in ('forwarded', 'x-forwarded-host', 'cookie', 'authorization',
                                               'cf-access-jwt-assertion'))
    assert r.headers['cache-control'] == 'private, no-store'


def test_session_fetch_never_injects_a_dashboard_token(gateway):
    client, config, origin, host, seen = gateway
    assert client.get('/api/session', headers={'Host': host}).status_code == 200
    assert 'x-pipeline-token' not in seen[0].headers
    assert seen[0].url.host == '127.0.0.1'


def test_oversized_body_is_rejected_without_forwarding(gateway, monkeypatch):
    from app import remote_gateway
    monkeypatch.setattr(remote_gateway, 'MAX_BODY', 8)
    client, config, origin, host, seen = gateway
    assert client.post('/api/profile', content=b'x' * 9,
                       headers={'Host': host, 'Origin': origin}).status_code == 413
    assert seen == []


def test_every_dashboard_page_api_and_download_requires_login(gateway):
    client, config, origin, host, seen = gateway
    client.cookies.clear()
    for path in ('/', '/static/app.js', '/practice', '/docs', '/openapi.json', '/data/secrets/anything'):
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers['location'].startswith('/login?next=')
    for path in ('/api/session', '/api/health', '/api/jobs', '/api/agent-artifacts/fixture', '/auth/logout'):
        assert client.get(path, follow_redirects=False).status_code == 401
    assert seen == []
    assert client.get('/login').status_code == 200
    assert client.get('/auth/assets/login.js').status_code == 200
    assert client.get('/auth/assets/anything-else').status_code == 401


def test_persistent_cookie_secure_flags_logout_and_revocation(gateway):
    client, config, origin, host, seen = gateway
    assert COOKIE in client.cookies
    login = client.post('/auth/login', headers={'Origin': origin},
                        json={'username': 'owner', 'password': 'synthetic long password for tests'})
    cookie = login.headers['set-cookie']
    assert all(flag in cookie for flag in ('Secure', 'HttpOnly', 'SameSite=lax', 'Max-Age=7776000', 'Path=/'))
    assert 'Domain=' not in cookie
    assert client.post('/auth/logout', headers={'Origin': origin}).status_code == 403
    status = client.get('/auth/status').json()
    original = client.cookies.get(COOKIE)
    assert client.post('/auth/logout', headers={'Origin': origin, 'X-DDoJA-CSRF': status['csrf']}).status_code == 200
    assert client.get('/api/session').status_code == 401
    assert client.get('/api/session', headers={'Cookie': COOKIE + '=' + original}).status_code == 401


def test_login_rejects_cross_site_forms_and_limits_attempts(gateway):
    client, config, origin, host, seen = gateway
    client.cookies.clear()
    secret = 'synthetic wrong password'
    assert client.post('/auth/login', headers={'Origin': 'https://attacker.example'},
                       json={'username': 'owner', 'password': secret}).status_code == 403
    assert client.post('/auth/login', headers={'Origin': origin}, data={'password': secret}).status_code == 415
    results = [client.post('/auth/login', headers={'Origin': origin},
                           json={'username': 'owner', 'password': secret}) for _ in range(9)]
    assert any(r.status_code == 429 and 'retry-after' in r.headers for r in results)
    assert all(secret not in r.text for r in results)
    assert seen == []


def test_cleartext_login_redirects_before_password_form_is_served(gateway):
    client, config, origin, host, seen = gateway
    settings = json.loads(config.read_text()); settings['transport'] = 'cloudflared'
    config.write_text(json.dumps(settings))
    response = client.get('/login', headers={'X-Forwarded-Proto': 'http'}, follow_redirects=False)
    assert response.status_code == 308 and response.headers['location'] == origin + '/login'
    assert b'<form' not in response.content and seen == []
    assert client.get('/login', headers={'X-Forwarded-Proto': 'https,http'}).status_code == 400

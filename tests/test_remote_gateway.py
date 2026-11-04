import json

import httpx
from fastapi.testclient import TestClient
import pytest

from app.remote_gateway import create_app


class FixtureStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b'fictional response'


@pytest.fixture
def gateway(tmp_path):
    config = tmp_path / 'gateway.json'
    origin = 'https://fictional-private-dashboard.trycloudflare.com'
    host = 'ddoja-' + 'a' * 64 + '.invalid'
    config.write_text(json.dumps({'enabled': True, 'origin': origin, 'ingress_host': host}))
    seen = []

    def upstream(request):
        seen.append(request)
        return httpx.Response(200, stream=FixtureStream(), headers={'content-type': 'text/plain'})

    with TestClient(create_app(config, transport=httpx.MockTransport(upstream))) as client:
        yield client, config, origin, host, seen


def test_disabled_gateway_and_unknown_connector_cannot_obtain_session(gateway):
    client, config, origin, host, seen = gateway
    assert client.get('/api/session').status_code == 403
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
        'Cookie': 'private-auth-cookie=synthetic', 'Authorization': 'Bearer synthetic',
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

"""Loopback-only adapter behind an email-protected cloudflared Quick Tunnel.

Authentication belongs to cloudflared's --allowed-mail gate. This adapter is
never a public listener and never retrieves/injects the dashboard's API token.
It validates the exact external Origin before translating it for the existing
local-only dashboard, so no dashboard restart or worker interruption is needed.
"""
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import re
import secrets

import httpx
from fastapi import FastAPI, Request
from starlette.background import BackgroundTask
from starlette.responses import JSONResponse, StreamingResponse

ORIGIN = re.compile(r"https://[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.trycloudflare\.com\Z")
INGRESS = re.compile(r"ddoja-[a-f0-9]{64}\.invalid\Z")
HOP_HEADERS = {'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
               'te', 'trailer', 'transfer-encoding', 'upgrade'}
LOCAL_ORIGIN = 'http://127.0.0.1:8765'
MAX_BODY = 16 * 1024 * 1024


def create_app(config_path=None, *, transport=None):
    config_path = Path(config_path or os.environ['DDOJA_REMOTE_CONFIG'])

    @asynccontextmanager
    async def lifespan(app):
        async with httpx.AsyncClient(transport=transport, trust_env=False,
                                    timeout=httpx.Timeout(60, connect=5),
                                    follow_redirects=False) as client:
            app.state.client = client
            yield

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    @app.api_route('/{path:path}', methods=['GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'])
    async def forward(request: Request, path: str):
        try:
            config = json.loads(config_path.read_text(encoding='utf-8'))
            origin, ingress_host = config['origin'], config['ingress_host']
            if (config.get('enabled') is not True or not ORIGIN.fullmatch(origin)
                    or not INGRESS.fullmatch(ingress_host)):
                raise ValueError('Remote access is not ready.')
        except (OSError, ValueError, KeyError, TypeError):
            return JSONResponse({'detail': 'Remote access is disabled.'}, status_code=503)

        # Only the launcher-owned connector supplies this random origin Host.
        # Public users are authenticated by that connector before forwarding.
        if not secrets.compare_digest(request.headers.get('host', ''), ingress_host):
            return JSONResponse({'detail': 'Protected connector required.'}, status_code=403)
        incoming_origin = request.headers.get('origin')
        if incoming_origin is not None and incoming_origin != origin:
            return JSONResponse({'detail': 'Cross-origin access is not allowed.'}, status_code=403)
        if request.method not in {'GET', 'HEAD', 'OPTIONS'} and incoming_origin != origin:
            return JSONResponse({'detail': 'The dashboard origin is required.'}, status_code=403)
        if request.headers.get('sec-fetch-site') == 'cross-site' and request.method not in {'GET', 'HEAD'}:
            return JSONResponse({'detail': 'Cross-site actions are not allowed.'}, status_code=403)

        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > MAX_BODY:
                return JSONResponse({'detail': 'Request is too large.'}, status_code=413)

        connection_headers = {v.strip().lower() for v in request.headers.get('connection', '').split(',')}
        omit = HOP_HEADERS | connection_headers | {'host', 'content-length', 'forwarded', 'cookie',
                                                   'authorization', 'proxy-connection'}
        headers = {k: v for k, v in request.headers.items()
                   if k.lower() not in omit and not k.lower().startswith(('x-forwarded-', 'cf-'))}
        headers['host'] = '127.0.0.1:8765'
        if incoming_origin:
            headers['origin'] = LOCAL_ORIGIN
        if 'referer' in headers:
            headers['referer'] = LOCAL_ORIGIN + '/'
        # Construct the upstream URL structurally: a request path can never
        # replace the fixed loopback destination (including //host paths).
        target = httpx.URL(scheme='http', host='127.0.0.1', port=8765,
                           path=request.url.path, query=request.url.query.encode('ascii'))
        client = request.app.state.client
        try:
            response = await client.send(httpx.Request(request.method, target,
                                         headers=headers, content=bytes(body)), stream=True)
        except httpx.HTTPError:
            return JSONResponse({'detail': 'Local dashboard is unavailable.'}, status_code=502)
        response_headers = {k: v for k, v in response.headers.items()
                            if k.lower() not in HOP_HEADERS | {'set-cookie'}}
        location = response_headers.get('location', '')
        if location == LOCAL_ORIGIN or location.startswith(LOCAL_ORIGIN + '/'):
            response_headers['location'] = origin + location[len(LOCAL_ORIGIN):]
        response_headers.update({'cache-control': 'private, no-store',
                                 'referrer-policy': 'no-referrer',
                                 'x-content-type-options': 'nosniff'})
        return StreamingResponse(response.aiter_raw(), status_code=response.status_code,
                                 headers=response_headers, background=BackgroundTask(response.aclose))

    return app

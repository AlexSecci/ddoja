"""Single-owner authenticated dashboard gateway; upstream stays on loopback."""
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import re
import secrets
from urllib.parse import urlsplit, quote

import httpx
from fastapi import FastAPI, Request
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from starlette.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse

from .dashboard_auth import DashboardAuth, AuthFailure, COOKIE, SESSION_SECONDS

ROOT = Path(__file__).resolve().parents[1]
INGRESS = re.compile(r'ddoja-[a-f0-9]{64}\.invalid\Z')
HOP_HEADERS = {'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
               'te', 'trailer', 'transfer-encoding', 'upgrade'}
LOCAL_ORIGIN = 'http://127.0.0.1:8765'
MAX_BODY = 16 * 1024 * 1024
PUBLIC_PATHS = {'/login', '/setup', '/auth/status', '/auth/login', '/auth/setup',
                '/auth/assets/login.js', '/auth/assets/login.css'}


def valid_origin(value):
    if not isinstance(value, str):
        return False
    try:
        parsed = urlsplit(value)
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            return False
    except ValueError:
        return False
    return (parsed.scheme == 'https' and parsed.netloc and not parsed.username and not parsed.password
            and not parsed.path and not parsed.query and not parsed.fragment
            and re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9.-]*(?::[0-9]{1,5})?', parsed.netloc) is not None)


def set_session(response, session):
    response.set_cookie(COOKIE, session['token'], max_age=SESSION_SECONDS,
                        secure=True, httponly=True, samesite='lax', path='/')


def create_app(config_path=None, *, transport=None, auth=None):
    config_path = Path(config_path or os.environ['DDOJA_REMOTE_CONFIG'])
    auth = auth or DashboardAuth(ROOT)

    @asynccontextmanager
    async def lifespan(app):
        async with httpx.AsyncClient(transport=transport, trust_env=False,
                                    timeout=httpx.Timeout(60, connect=5),
                                    follow_redirects=False) as client:
            app.state.client = client
            yield

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.auth = auth

    @app.middleware('http')
    async def authenticated_access(request, call_next):
        try:
            config = json.loads(config_path.read_text(encoding='utf-8'))
            origin, ingress = config['origin'], config['ingress_host']
            if (config.get('enabled') is not True or config.get('authentication') != 'password'
                    or not valid_origin(origin) or not isinstance(ingress, str) or not INGRESS.fullmatch(ingress)):
                raise ValueError('Remote access is disabled.')
        except (OSError, ValueError, KeyError, TypeError):
            return JSONResponse({'detail': 'Remote access is disabled.'}, status_code=503)
        if request.headers.get('host', '') != ingress:
            return JSONResponse({'detail': 'Dashboard gateway required.'}, status_code=403)
        # Cloudflare overwrites X-Forwarded-Proto with the visitor protocol.
        # Trust it only after the private connector Host check, never from a
        # public listener. Do not serve a password form over cleartext HTTP.
        if config.get('transport') == 'cloudflared':
            protocol = request.headers.get('x-forwarded-proto', 'https')
            if protocol == 'http':
                suffix = request.url.path
                if request.url.query:
                    suffix += '?' + request.url.query
                return RedirectResponse(origin + suffix, status_code=308)
            if protocol != 'https':
                return JSONResponse({'detail': 'HTTPS is required.'}, status_code=400)
        incoming_origin = request.headers.get('origin')
        if incoming_origin is not None and incoming_origin != origin:
            return JSONResponse({'detail': 'Cross-origin access is not allowed.'}, status_code=403)
        if request.method not in {'GET', 'HEAD', 'OPTIONS'}:
            if incoming_origin != origin or request.headers.get('sec-fetch-site') == 'cross-site':
                return JSONResponse({'detail': 'The dashboard origin is required.'}, status_code=403)
        request.state.remote_config = config
        session = await run_in_threadpool(auth.authenticate, request.cookies.get(COOKIE))
        request.state.remote_session = session
        path = request.url.path
        if path not in PUBLIC_PATHS and session is None:
            if path.startswith(('/api/', '/auth/')):
                response = JSONResponse({'detail': 'Sign in to continue.', 'login_required': True}, status_code=401)
            else:
                response = RedirectResponse('/login?next=' + quote(path, safe=''), status_code=303)
        else:
            response = await call_next(request)
        if session and session['renew'] and path not in {'/auth/login', '/auth/setup', '/auth/logout'}:
            set_session(response, session)
        response.headers.update({'Cache-Control': 'private, no-store', 'Referrer-Policy': 'no-referrer',
            'X-Content-Type-Options': 'nosniff', 'X-Frame-Options': 'DENY',
            'Strict-Transport-Security': 'max-age=31536000',
            'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"})
        return response

    @app.exception_handler(AuthFailure)
    async def auth_error(request, error):
        headers = {'Retry-After': str(error.retry_after)} if error.retry_after else {}
        return JSONResponse({'detail': error.detail}, status_code=error.status, headers=headers)

    @app.api_route('/login', methods=['GET', 'HEAD'])
    @app.api_route('/setup', methods=['GET', 'HEAD'])
    async def login_page(request: Request):
        if request.state.remote_session:
            return RedirectResponse('/', status_code=303)
        return FileResponse(ROOT / 'app/static/dashboard-login.html')

    @app.get('/auth/assets/{name}')
    async def login_asset(name: str):
        if name not in {'login.js', 'login.css'}:
            return JSONResponse({'detail': 'Not found.'}, status_code=404)
        return FileResponse(ROOT / 'app/static' / ('dashboard-' + name),
                            media_type='application/javascript' if name.endswith('.js') else 'text/css')

    @app.get('/auth/status')
    async def auth_status(request: Request):
        session = request.state.remote_session
        return {'configured': await run_in_threadpool(auth.configured), 'authenticated': bool(session),
                **({'username': session['username'], 'csrf': session['csrf']} if session else {})}

    async def credentials(request):
        if request.headers.get('content-type', '').split(';', 1)[0].strip() != 'application/json':
            raise AuthFailure('Use the sign-in form.', 415)
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 16384:
                raise AuthFailure('The sign-in request is too large.', 413)
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError()
            return data
        except (ValueError, UnicodeError):
            raise AuthFailure('The sign-in request is invalid.', 400) from None

    def client_key(request):
        if request.state.remote_config.get('transport') == 'cloudflared':
            return request.headers.get('cf-connecting-ip', 'unknown')[:100]
        return request.client.host if request.client else 'unknown'

    @app.post('/auth/login')
    async def login(request: Request):
        data = await credentials(request)
        session = await run_in_threadpool(auth.login, data.get('username'), data.get('password'), client_key(request))
        response = JSONResponse({'authenticated': True})
        set_session(response, session)
        return response

    @app.post('/auth/setup')
    async def setup(request: Request):
        data = await credentials(request)
        session = await run_in_threadpool(auth.setup, data.get('setup_token'), data.get('username'),
                                          data.get('password'), client_key(request))
        response = JSONResponse({'authenticated': True})
        set_session(response, session)
        return response

    @app.post('/auth/logout')
    async def logout(request: Request):
        session = request.state.remote_session
        if not secrets.compare_digest(request.headers.get('x-ddoja-csrf', '').encode('utf-8'), session['csrf'].encode('ascii')):
            raise AuthFailure('Refresh the dashboard before signing out.', 403)
        await run_in_threadpool(auth.logout, session['token'])
        response = JSONResponse({'authenticated': False})
        response.delete_cookie(COOKIE, secure=True, httponly=True, samesite='lax', path='/')
        return response

    @app.api_route('/{path:path}', methods=['GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'])
    async def forward(request: Request, path: str):
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > MAX_BODY:
                return JSONResponse({'detail': 'Request is too large.'}, status_code=413)
        connection_headers = {v.strip().lower() for v in request.headers.get('connection', '').split(',')}
        omit = HOP_HEADERS | connection_headers | {'host', 'content-length', 'forwarded', 'cookie',
                                                   'authorization', 'proxy-connection', 'x-ddoja-csrf'}
        headers = {k: v for k, v in request.headers.items()
                   if k.lower() not in omit and not k.lower().startswith(('x-forwarded-', 'cf-'))}
        headers['host'] = '127.0.0.1:8765'
        if request.headers.get('origin'):
            headers['origin'] = LOCAL_ORIGIN
        if 'referer' in headers:
            headers['referer'] = LOCAL_ORIGIN + '/'
        target = httpx.URL(scheme='http', host='127.0.0.1', port=8765,
                           path=request.url.path, query=request.url.query.encode('ascii'))
        try:
            response = await request.app.state.client.send(httpx.Request(request.method, target,
                                            headers=headers, content=bytes(body)), stream=True)
        except httpx.HTTPError:
            return JSONResponse({'detail': 'Local dashboard is unavailable.'}, status_code=502)
        response_headers = {k: v for k, v in response.headers.items()
                            if k.lower() not in HOP_HEADERS | {'set-cookie'}}
        location = response_headers.get('location', '')
        if location == LOCAL_ORIGIN or location.startswith(LOCAL_ORIGIN + '/'):
            response_headers['location'] = request.state.remote_config['origin'] + location[len(LOCAL_ORIGIN):]
        return StreamingResponse(response.aiter_raw(), status_code=response.status_code,
                                 headers=response_headers, background=BackgroundTask(response.aclose))

    return app

"""Opt-in public HTTPS/browser check with disposable auth and read-only access.

The real campaign scheduler and controls are untouched. All upstream methods
except GET/HEAD are rejected, even after logging into the disposable gateway.
"""
import argparse
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time
import urllib.request
import urllib.parse
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.dashboard_auth import DashboardAuth
from app.browser_sessions import process_identity
from tools.remote_dashboard import verified_binary, stop_owned, PUBLIC_URL, save


def run():
    folder = ROOT / 'data/remote-dashboard/smoke' / uuid.uuid4().hex
    folder.mkdir(parents=True)
    config_path = folder / 'gateway.json'
    config = {'enabled': False, 'origin': '', 'authentication': 'password', 'transport': 'cloudflared',
              'ingress_host': 'ddoja-' + secrets.token_hex(32) + '.invalid'}
    save(config_path, config)
    auth = DashboardAuth(folder)
    setup_token = auth.prepare_setup()
    module = '''import httpx
from app.remote_gateway import create_app as gateway
from app.dashboard_auth import DashboardAuth
class ReadOnly(httpx.AsyncHTTPTransport):
    async def handle_async_request(self,request):
        if request.method not in ('GET','HEAD'):
            return httpx.Response(405,content=b'Read-only test gateway')
        return await super().handle_async_request(request)
def create_app():
    return gateway(CONFIG_PATH,auth=DashboardAuth(AUTH_ROOT),transport=ReadOnly())
'''.replace('CONFIG_PATH', repr(str(config_path))).replace('AUTH_ROOT', repr(str(folder)))
    (folder / 'smoke_gateway.py').write_text(module, encoding='utf-8')
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0)); port = s.getsockname()[1]
    env = dict(os.environ, PYTHONPATH=str(folder) + os.pathsep + str(ROOT))
    children, handles = [], []
    private = folder / 'client.json'
    result = {'passed': False, 'readonly_upstream': True, 'campaign_controls_changed': False}
    def launch(args, name):
        out = (folder / (name + '.stdout.log')).open('w', encoding='utf-8')
        err = (folder / (name + '.stderr.log')).open('w', encoding='utf-8')
        handles.extend([out, err])
        child = subprocess.Popen(args, cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                                 stdout=out, stderr=err, creationflags=subprocess.CREATE_NO_WINDOW)
        children.append((child, process_identity(child.pid)))
        return child
    def wait_gateway():
        for _ in range(60):
            try:
                with socket.create_connection(('127.0.0.1', port), timeout=.3):return
            except OSError:time.sleep(.2)
        raise RuntimeError('Fixture gateway did not start.')
    args = [sys.executable, '-m', 'uvicorn', 'smoke_gateway:create_app', '--factory',
            '--host', '127.0.0.1', '--port', str(port), '--no-access-log', '--no-proxy-headers']
    try:
        gateway = launch(args, 'gateway'); wait_gateway()
        connector = launch([str(verified_binary()), 'tunnel', '--url', f'http://127.0.0.1:{port}',
                            '--http-host-header', config['ingress_host'], '--no-autoupdate', '--output', 'json'], 'connector')
        for _ in range(150):
            text = '\n'.join((folder / ('connector.' + name + '.log')).read_text(encoding='utf-8', errors='replace') for name in ('stdout', 'stderr'))
            match = PUBLIC_URL.search(text)
            if match:break
            if connector.poll() is not None:raise RuntimeError('Fixture connector exited.')
            time.sleep(.3)
        else:raise RuntimeError('Fixture connector did not provide its URL.')
        config.update(enabled=True, origin=match.group(0)); save(config_path, config)
        request = urllib.request.Request(f'http://127.0.0.1:{port}/auth/status',
                                         headers={'Host': config['ingress_host']})
        with urllib.request.urlopen(request, timeout=5) as response:
            assert json.load(response)['authenticated'] is False
        # A freshly assigned hostname can precede DNS/edge readiness. Wait for
        # an actual JSON response, not merely the connector's URL announcement.
        # Query DNS over HTTPS first so an early NXDOMAIN does not poison the
        # local resolver's negative cache for this newly assigned hostname.
        host = urllib.parse.urlsplit(config['origin']).hostname
        for _ in range(45):
            try:
                request = urllib.request.Request('https://dns.google/resolve?name=' + host + '&type=A',
                    headers={'User-Agent': 'DDoJA/1.0 (remote dashboard acceptance)'})
                with urllib.request.urlopen(request, timeout=5) as response:
                    answer = json.load(response)
                if any(item.get('type') == 1 for item in answer.get('Answer', [])):
                    break
            except OSError:
                pass
            time.sleep(1)
        last_error = None
        for _ in range(60):
            try:
                request = urllib.request.Request(config['origin'] + '/auth/status',
                    headers={'User-Agent': 'DDoJA/1.0 (remote dashboard acceptance)', 'Accept': 'application/json'})
                with urllib.request.urlopen(request, timeout=5) as response:
                    if json.load(response).get('authenticated') is False:
                        break
            except (OSError, ValueError) as error:
                last_error = {'type': type(error).__name__, 'status': getattr(error, 'code', None),
                              'reason': str(getattr(error, 'reason', ''))[:250]}
                if hasattr(error, 'read'):
                    last_error['detail'] = error.read(300).decode('utf-8', errors='replace')
            if _ in (0, 10, 30):
                print(json.dumps({'public_readiness': last_error}), flush=True)
            time.sleep(1)
        else:
            raise RuntimeError('The public fixture URL did not become ready: ' + json.dumps(last_error))
        client = {'url': config['origin'], 'setup_token': setup_token, 'username': 'smoke-owner',
                  'password': secrets.token_urlsafe(32), 'storage': str(folder / 'browser-state.json'),
                  'browser': r'C:\Program Files\Google\Chrome\Application\chrome.exe'}
        save(private, client)
        result['phases'] = []
        for phase in ('setup', 'resume'):
            if phase == 'resume':
                item = next(item for item in children if item[0] is gateway)
                stop_owned(*item); children.remove(item)
                gateway = launch(args, 'gateway-restarted'); wait_gateway()
            completed = subprocess.run(['node', str(ROOT / 'tests/remote_dashboard_live_ui.cjs'), str(private), phase],
                                       cwd=ROOT, capture_output=True, text=True, encoding='utf-8', timeout=150)
            if not completed.stdout.strip():
                detail = completed.stderr[-2000:]
                for secret in (client['password'], client['setup_token']):
                    detail = detail.replace(secret, '[redacted]')
                raise RuntimeError('Browser runner returned no report: ' + str(completed.returncode) + ' ' + detail)
            report = json.loads(completed.stdout.strip().splitlines()[-1])
            result['phases'].append(report)
            print(json.dumps(report), flush=True)
            if completed.returncode != 0:raise RuntimeError('Live browser check failed.')
        result.update(passed=True, public_https_verified=True, gateway_restart_session_persisted=True)
    finally:
        config['enabled'] = False; save(config_path, config)
        for child, identity in reversed(children):
            stop_owned(child, identity)
        for handle in handles:handle.close()
        private.unlink(missing_ok=True)
        (folder / 'browser-state.json').unlink(missing_ok=True)
        save(folder / 'acceptance.json', result)
        print(json.dumps({'passed': result['passed'], 'report': str(folder / 'acceptance.json')}), flush=True)
    return 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-live-check', action='store_true', required=True)
    parser.parse_args()
    raise SystemExit(run())

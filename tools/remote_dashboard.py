"""Start/stop a password-protected HTTPS dashboard without admin rights.

Requires data/bin/cloudflared.exe and its verified release download record.
The tunnel transports HTTPS only. DDoJA owns the single-user login.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.dashboard_auth import DashboardAuth
from app.browser_sessions import process_identity
from app.provider_credentials import _dpapi
import base64

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / 'data/remote-dashboard'
CURRENT = STATE / 'current.json'
PUBLIC_URL = re.compile(r'https://[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.trycloudflare\.com\b')


def save(path, data):
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(data, indent=2), encoding='utf-8')
    temp.replace(path)


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def verified_binary():
    binary = ROOT / 'data/bin/cloudflared.exe'
    audit = read(ROOT / 'data/maintenance/remote-dashboard-download.json')
    if (not audit.get('digest_verified') or not audit['asset'].startswith(
            'https://github.com/cloudflare/cloudflared/releases/download/')
            or hashlib.sha256(binary.read_bytes()).hexdigest() != audit['sha256']):
        raise RuntimeError('The connector release checksum has not been verified.')
    help_text = subprocess.run([str(binary), 'tunnel', '--help'], capture_output=True,
                              text=True, encoding='utf-8', timeout=15, check=True).stdout
    if '--http-host-header' not in help_text:
        raise RuntimeError('This connector cannot isolate the dashboard origin.')
    return binary


def stop_owned(child, identity):
    if child.poll() is not None:
        return
    if process_identity(child.pid) != identity:
        raise RuntimeError('The owned process identity changed; no process was stopped.')
    if os.name == 'nt':
        # This is the still-live Popen child, never a PID loaded from a file.
        # /T includes the Windows venv launcher child as well as its wrapper.
        subprocess.run(['taskkill', '/PID', str(child.pid), '/T', '/F'],
                       capture_output=True, timeout=15, check=False)
    else:
        child.terminate()
    child.wait(timeout=15)


def setup_link(origin):
    auth = DashboardAuth(ROOT)
    if auth.configured():
        return None
    token = auth.prepare_setup()
    path = ROOT / 'data/secrets/dashboard-setup.dpapi'
    path.write_bytes(base64.b64encode(_dpapi(token.encode('utf-8'))))
    return origin + '/setup#' + token


def wait_public_ready(origin, timeout=60):
    """Wait for DNS publication before the first native lookup can cache NXDOMAIN."""
    host = urllib.parse.urlsplit(origin).hostname
    deadline = time.monotonic() + timeout
    dns_ready = False
    headers = {'User-Agent': 'DDoJA/1.0 (remote dashboard readiness)', 'Accept': 'application/json'}
    while time.monotonic() < deadline:
        try:
            if not dns_ready:
                request = urllib.request.Request('https://dns.google/resolve?name=' + host + '&type=A', headers=headers)
                with urllib.request.urlopen(request, timeout=5) as response:
                    dns_ready = any(item.get('type') == 1 for item in json.load(response).get('Answer', []))
            if dns_ready:
                request = urllib.request.Request(origin + '/auth/status', headers=headers)
                with urllib.request.urlopen(request, timeout=5) as response:
                    status = json.load(response)
                if isinstance(status.get('authenticated'), bool):
                    return
        except (OSError, ValueError):
            pass
        time.sleep(1)
    raise RuntimeError('The public HTTPS address is not ready yet.')


def serve(run_id):
    import msvcrt
    STATE.mkdir(parents=True, exist_ok=True)
    folder = STATE / 'runs' / run_id
    folder.mkdir(parents=True, exist_ok=True)
    lock = (STATE / 'server.lock').open('a+b')
    lock.seek(0)
    if lock.read(1) == b'':
        lock.write(b'0'); lock.flush()
    lock.seek(0)
    try:
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        raise RuntimeError('An existing remote gateway still owns the launch lock.') from None
    processes = []
    handles = []
    info = {'run_id': run_id, 'status': 'starting',
            'started_at': datetime.now(timezone.utc).isoformat(), 'supervisor_pid': os.getpid(),
            'supervisor_identity': process_identity(os.getpid()),
            'run_folder': str(folder), 'authenticated_phone_verified': False}
    config_path = folder / 'gateway.json'
    try:
        binary = verified_binary()
        with urllib.request.urlopen('http://127.0.0.1:8765/api/health', timeout=5) as r:
            if json.load(r).get('status') != 'ok':
                raise RuntimeError('The local dashboard is unavailable.')
        save(CURRENT, info)
        # The socket selection has no public listener; uvicorn binds only loopback.
        with socket.socket() as s:
            s.bind(('127.0.0.1', 0)); port = s.getsockname()[1]
        config = {'enabled': False, 'origin': '',
                  'ingress_host': 'ddoja-' + secrets.token_hex(32) + '.invalid',
                  'authentication': 'password', 'transport': 'cloudflared'}
        # Initialize local authentication before making any origin reachable.
        DashboardAuth(ROOT)
        save(config_path, config)

        def launch(args, name, env=None):
            out = (folder / (name + '.stdout.log')).open('w', encoding='utf-8')
            err = (folder / (name + '.stderr.log')).open('w', encoding='utf-8')
            handles.extend([out, err])
            child = subprocess.Popen(args, cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                                     stdout=out, stderr=err, creationflags=subprocess.CREATE_NO_WINDOW)
            processes.append((child, process_identity(child.pid)))
            return child

        env = dict(os.environ, DDOJA_REMOTE_CONFIG=str(config_path))
        gateway_args = [sys.executable, '-m', 'uvicorn', 'app.remote_gateway:create_app',
                          '--factory', '--host', '127.0.0.1', '--port', str(port),
                          '--no-access-log', '--no-proxy-headers']
        gateway = launch(gateway_args, 'gateway', env)
        deadline = time.monotonic() + 15
        while True:
            if gateway.poll() is not None:
                raise RuntimeError('The loopback gateway did not start.')
            try:
                with socket.create_connection(('127.0.0.1', port), timeout=1):
                    break
            except OSError:
                if time.monotonic() > deadline:
                    raise RuntimeError('The loopback gateway did not become ready.')
                time.sleep(.2)
        connector = launch([str(binary), 'tunnel', '--url', f'http://127.0.0.1:{port}',
                            '--http-host-header', config['ingress_host'],
                            '--no-autoupdate', '--output', 'json'], 'connector')
        deadline = time.monotonic() + 50
        while True:
            if connector.poll() is not None:
                raise RuntimeError('The protected connector did not start.')
            logs = '\n'.join((folder / ('connector.' + kind + '.log')).read_text(
                encoding='utf-8', errors='replace') for kind in ('stdout', 'stderr'))
            match = PUBLIC_URL.search(logs)
            if match:
                origin = match.group(0)
                break
            if time.monotonic() > deadline:
                raise RuntimeError('The protected connector did not provide an HTTPS URL.')
            time.sleep(.3)
        config.update(enabled=True, origin=origin)
        save(config_path, config)
        info.update(status='ready', url=origin, gateway_pid=gateway.pid,
                    connector_pid=connector.pid, gateway_port=port,
                    authentication='DDoJA username/password; persistent 90-day cookie',
                    transport='cloudflared (HTTPS only; no Access)',
                    dashboard_restarted=False, campaign_controls_changed=False)
        save(CURRENT, info)
        while not (folder / 'stop.request').exists():
            reload_path = folder / 'reload.request'
            if reload_path.exists():
                reload_path.unlink()
                previous = next(item for item in processes if item[0] is gateway)
                stop_owned(*previous)
                processes.remove(previous)
                for _ in range(30):
                    try:
                        with socket.create_connection(('127.0.0.1', port), timeout=.2):
                            time.sleep(.1)
                    except OSError:
                        break
                else:
                    raise RuntimeError('The previous gateway did not release its port.')
                gateway = launch(gateway_args, 'gateway', env)
                info['gateway_pid'] = gateway.pid
                info['gateway_restarted_at'] = datetime.now(timezone.utc).isoformat()
                save(CURRENT, info)
            if any(child.poll() is not None for child, _ in processes):
                raise RuntimeError('A remote-access child process exited; access disabled.')
            time.sleep(.5)
        info['status'] = 'stopped'
    except BaseException as error:
        info.update(status='failed', error=str(error))
        raise
    finally:
        if config_path.exists():
            config = read(config_path); config['enabled'] = False; save(config_path, config)
        for child, identity in reversed(processes):
            if child.poll() is None:
                stop_owned(child, identity)
        for handle in handles:
            handle.close()
        info['finished_at'] = datetime.now(timezone.utc).isoformat()
        save(CURRENT, info)
        lock.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['start', 'serve', 'status', 'stop', 'reload', 'setup-link', 'configure'])
    parser.add_argument('--username', default='admin')
    parser.add_argument('--run-id')
    args = parser.parse_args()
    STATE.mkdir(parents=True, exist_ok=True)
    if args.action == 'serve':
        if not args.run_id or not re.fullmatch('[a-f0-9]{32}', args.run_id):
            parser.error('A generated run ID is required.')
        serve(args.run_id)
    elif args.action == 'start':
        run_id = uuid.uuid4().hex
        with (STATE / 'supervisor.stdout.log').open('a', encoding='utf-8') as out, (
                STATE / 'supervisor.stderr.log').open('a', encoding='utf-8') as err:
            child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), 'serve',
                                      '--run-id', run_id], cwd=ROOT,
                                     stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                                     creationflags=subprocess.CREATE_NO_WINDOW)
        deadline = time.monotonic() + 75
        while time.monotonic() < deadline:
            info = read(CURRENT) if CURRENT.exists() else {}
            if info.get('run_id') == run_id and info.get('status') == 'ready':
                try:
                    wait_public_ready(info['url'])
                except RuntimeError:
                    (STATE / 'runs' / run_id / 'stop.request').touch()
                    raise SystemExit('Public HTTPS was not ready; gateway shutdown requested.') from None
                print(json.dumps({'status': 'ready', 'url': info['url'],
                                  'setup_url': setup_link(info['url']),
                                  'authenticated_phone_verified': False})); return
            if child.poll() is not None:
                raise SystemExit('Protected remote access did not start; inspect local supervisor logs.')
            time.sleep(.3)
        # The process observes its own run-scoped stop request; never kill a saved PID.
        (STATE / 'runs' / run_id / 'stop.request').touch()
        raise SystemExit('Remote launch timed out; shutdown requested.')
    elif args.action in {'stop', 'reload'}:
        info = read(CURRENT)
        run_id = info['run_id']
        if not re.fullmatch('[a-f0-9]{32}', run_id):
            raise SystemExit('Invalid saved run identity; no process was stopped.')
        if process_identity(info['supervisor_pid']) != info.get('supervisor_identity'):
            # Older supervisors use run-scoped stop files too. A stale run
            # cannot affect any future gateway because its directory is unique.
            if args.action == 'reload':
                raise SystemExit('The gateway supervisor is unavailable. Start it again.')
        (STATE / 'runs' / run_id / ('stop.request' if args.action == 'stop' else 'reload.request')).touch()
        print('Gateway ' + args.action + ' requested; campaigns are unchanged.')
    elif args.action == 'setup-link':
        info = read(CURRENT)
        if info.get('status') != 'ready':
            raise SystemExit('Start the remote gateway first.')
        link = setup_link(info['url'])
        print(link if link else 'The owner account is already configured.')
    elif args.action == 'configure':
        import getpass
        password = getpass.getpass('New dashboard password (15+ characters): ')
        if password != getpass.getpass('Repeat password: '):
            raise SystemExit('Passwords did not match. Nothing changed.')
        DashboardAuth(ROOT).configure(args.username, password)
        print('Owner credentials saved. All previous remote logins were revoked.')
    else:
        info = read(CURRENT) if CURRENT.exists() else {'status': 'not_configured'}
        if info.get('supervisor_identity') and process_identity(info['supervisor_pid']) != info['supervisor_identity']:
            info['status'] = 'not_running'
        print(json.dumps({k: info.get(k) for k in ('status', 'url', 'authentication',
                         'authenticated_phone_verified', 'error')}))


if __name__ == '__main__':
    main()

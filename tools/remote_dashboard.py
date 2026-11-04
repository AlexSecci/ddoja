"""Start/stop an email-protected temporary HTTPS dashboard without admin rights.

Requires data/bin/cloudflared.exe and its verified release download record.
No unprotected tunnel mode, service installation, router or firewall changes.
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

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / 'data/remote-dashboard'
CURRENT = STATE / 'current.json'
EMAIL = re.compile(r'[A-Za-z0-9.!#$%&\x27+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+\Z')
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
    if '--allowed-mail' not in help_text:
        raise RuntimeError('This connector cannot enforce email authentication.')
    return binary


def serve(run_id, email):
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
    info = {'run_id': run_id, 'status': 'starting', 'email': email,
            'started_at': datetime.now(timezone.utc).isoformat(), 'supervisor_pid': os.getpid(),
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
                  'ingress_host': 'ddoja-' + secrets.token_hex(32) + '.invalid'}
        save(config_path, config)

        def launch(args, name, env=None):
            out = (folder / (name + '.stdout.log')).open('w', encoding='utf-8')
            err = (folder / (name + '.stderr.log')).open('w', encoding='utf-8')
            handles.extend([out, err])
            child = subprocess.Popen(args, cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                                     stdout=out, stderr=err, creationflags=subprocess.CREATE_NO_WINDOW)
            processes.append(child)
            return child

        env = dict(os.environ, DDOJA_REMOTE_CONFIG=str(config_path))
        gateway = launch([sys.executable, '-m', 'uvicorn', 'app.remote_gateway:create_app',
                          '--factory', '--host', '127.0.0.1', '--port', str(port),
                          '--no-access-log', '--no-proxy-headers'], 'gateway', env)
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
                            '--allowed-mail', email, '--http-host-header', config['ingress_host'],
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
                    authentication='cloudflared --allowed-mail, exact email only',
                    dashboard_restarted=False, campaign_controls_changed=False)
        save(CURRENT, info)
        while not (folder / 'stop.request').exists():
            if any(child.poll() is not None for child in processes):
                raise RuntimeError('A remote-access child process exited; access disabled.')
            time.sleep(.5)
        info['status'] = 'stopped'
    except BaseException as error:
        info.update(status='failed', error=str(error))
        raise
    finally:
        if config_path.exists():
            config = read(config_path); config['enabled'] = False; save(config_path, config)
        for child in reversed(processes):
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    child.kill(); child.wait(timeout=5)
        for handle in handles:
            handle.close()
        info['finished_at'] = datetime.now(timezone.utc).isoformat()
        save(CURRENT, info)
        lock.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['start', 'serve', 'status', 'stop'])
    parser.add_argument('--email')
    parser.add_argument('--run-id')
    args = parser.parse_args()
    STATE.mkdir(parents=True, exist_ok=True)
    if args.action in {'start', 'serve'}:
        if not args.email or not EMAIL.fullmatch(args.email) or any(c in args.email for c in '*,;'):
            parser.error('One exact authorized email address is required; wildcards are not supported.')
    if args.action == 'serve':
        if not args.run_id or not re.fullmatch('[a-f0-9]{32}', args.run_id):
            parser.error('A generated run ID is required.')
        serve(args.run_id, args.email)
    elif args.action == 'start':
        run_id = uuid.uuid4().hex
        with (STATE / 'supervisor.stdout.log').open('a', encoding='utf-8') as out, (
                STATE / 'supervisor.stderr.log').open('a', encoding='utf-8') as err:
            child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), 'serve',
                                      '--run-id', run_id, '--email', args.email], cwd=ROOT,
                                     stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                                     creationflags=subprocess.CREATE_NO_WINDOW)
        deadline = time.monotonic() + 75
        while time.monotonic() < deadline:
            info = read(CURRENT) if CURRENT.exists() else {}
            if info.get('run_id') == run_id and info.get('status') == 'ready':
                print(json.dumps({'status': 'ready', 'url': info['url'], 'email': info['email'],
                                  'authenticated_phone_verified': False})); return
            if child.poll() is not None:
                raise SystemExit('Protected remote access did not start; inspect local supervisor logs.')
            time.sleep(.3)
        # The process observes its own run-scoped stop request; never kill a saved PID.
        (STATE / 'runs' / run_id / 'stop.request').touch()
        raise SystemExit('Remote launch timed out; shutdown requested.')
    elif args.action == 'stop':
        info = read(CURRENT)
        run_id = info['run_id']
        if not re.fullmatch('[a-f0-9]{32}', run_id):
            raise SystemExit('Invalid saved run identity; no process was stopped.')
        (STATE / 'runs' / run_id / 'stop.request').touch()
        print('Stop requested for this gateway only; campaigns are unchanged.')
    else:
        info = read(CURRENT) if CURRENT.exists() else {'status': 'not_configured'}
        print(json.dumps({k: info.get(k) for k in ('status', 'url', 'email', 'authentication',
                         'authenticated_phone_verified', 'error')}))


if __name__ == '__main__':
    main()

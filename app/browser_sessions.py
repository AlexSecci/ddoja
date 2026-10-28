"""Worker-owned browser services, parented by the dashboard rather than Codex.

The MCP HTTP service owns one shared context *inside that worker's process*.
Disconnecting a CLI client does not close its pages. Other workers get separate
processes, profiles and ports. A slow live service is never silently restarted.
"""
from contextlib import contextmanager
import ctypes
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import threading
import time
import uuid

_LOCK = threading.RLock()


def process_identity(pid):
    """Creation identity as well as liveness; a reused PID is never an owner."""
    if os.name == 'nt':
        from ctypes import wintypes
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
        kernel.OpenProcess.restype=wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes=[wintypes.HANDLE,ctypes.POINTER(wintypes.DWORD)]
        kernel.GetProcessTimes.argtypes=[wintypes.HANDLE]+[ctypes.POINTER(wintypes.FILETIME)]*4
        kernel.CloseHandle.argtypes=[wintypes.HANDLE]
        handle=kernel.OpenProcess(0x1000,False,pid)
        if not handle:return None
        try:
            code=wintypes.DWORD()
            if not kernel.GetExitCodeProcess(handle,ctypes.byref(code)) or code.value!=259:return None
            times=[wintypes.FILETIME() for _ in range(4)]
            if not kernel.GetProcessTimes(handle,*(ctypes.byref(t) for t in times)):return None
            return str((times[0].dwHighDateTime<<32)|times[0].dwLowDateTime)
        finally:kernel.CloseHandle(handle)
    try:
        os.kill(pid,0)
        return Path(f'/proc/{pid}/stat').read_text().split(') ',1)[1].split()[19]
    except (OSError,IndexError):return None


@contextmanager
def owner_lock(folder):
    folder.mkdir(parents=True,exist_ok=True)
    with _LOCK, (folder/'owner.lock').open('a+b') as file:
        file.seek(0);file.write(b'0');file.flush();file.seek(0)
        deadline=time.monotonic()+30
        while True:
            try:
                if os.name=='nt':
                    import msvcrt
                    msvcrt.locking(file.fileno(),msvcrt.LK_NBLCK,1)
                else:
                    import fcntl
                    fcntl.flock(file,fcntl.LOCK_EX|fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic()>=deadline:raise RuntimeError('Browser owner is busy; no replacement was started.')
                time.sleep(.05)
        try:yield
        finally:
            file.seek(0)
            if os.name=='nt':msvcrt.locking(file.fileno(),msvcrt.LK_UNLCK,1)
            else:fcntl.flock(file,fcntl.LOCK_UN)


class BrowserSessions:
    def __init__(self,root):self.root=Path(root).resolve()

    def folder(self,worker_id):
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',worker_id):raise ValueError('Invalid browser owner.')
        return self.root/'data/browser-profiles'/worker_id

    def inspect(self,worker_id):
        path=self.folder(worker_id)/'session.json'
        try:value=json.loads(path.read_text(encoding='utf-8'))
        except (OSError,ValueError):return {'state':'absent','worker_id':worker_id}
        if value.get('worker_id')!=worker_id or value.get('root')!=str(self.root):
            raise RuntimeError('Browser owner metadata does not match this workspace.')
        live=process_identity(value['pid'])==value.get('process_identity')
        return {**value,'state':'live' if live else 'lost','live_state_preserved':live and value.get('live_state_preserved',False)}

    @staticmethod
    def reachable(value):
        try:
            with socket.create_connection(('127.0.0.1',value['port']),timeout=1):return True
        except OSError:return False

    def ensure(self,worker_id,status):
        folder=self.folder(worker_id)
        with owner_lock(folder):
            old=self.inspect(worker_id)
            if old['state']=='live':
                if not self.reachable(old):raise RuntimeError('The owned browser service is live but unavailable; preserve it for recovery.')
                old['live_state_preserved']=True
                (folder/'session.json').write_text(json.dumps(old),encoding='utf-8')
                return {**old,'reused':True}
            with socket.socket() as sock:
                sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
            config={'browser':{'browserName':'chromium','userDataDir':str(folder/'profile'),
                'launchOptions':{'executablePath':status['browser'],'headless':True,'chromiumSandbox':True}},
                'outputDir':str(folder/'output')}
            config_path=folder/'service-config.json'
            config_path.write_text(json.dumps(config),encoding='utf-8')
            args=[status['node'],status['cli'],'--config',str(config_path),'--port',str(port),
                  '--host','127.0.0.1','--shared-browser-context','--idle-timeout','0']
            flags=(subprocess.CREATE_NEW_PROCESS_GROUP|subprocess.CREATE_NO_WINDOW) if os.name=='nt' else 0
            with (folder/'service.stdout.log').open('ab') as out,(folder/'service.stderr.log').open('ab') as err:
                process=subprocess.Popen(args,stdin=subprocess.DEVNULL,stdout=out,stderr=err,
                    cwd=self.root,creationflags=flags,start_new_session=os.name!='nt')
            value={'worker_id':worker_id,'root':str(self.root),'pid':process.pid,'process_identity':process_identity(process.pid),
                   'port':port,'endpoint':f'http://localhost:{port}/mcp','generation':uuid.uuid4().hex,
                   'started_at_unix':time.time(),'state':'live','live_state_preserved':False,'previous_generation':old.get('generation')}
            temporary=folder/'session.tmp'
            temporary.write_text(json.dumps(value),encoding='utf-8');temporary.replace(folder/'session.json')
            deadline=time.monotonic()+20
            while not self.reachable(value):
                if process.poll() is not None:raise RuntimeError('Owned browser service exited during startup; see its private service log.')
                if time.monotonic()>=deadline:raise RuntimeError('Owned browser service is still starting; retain its live process for recovery.')
                time.sleep(.05)
            return {**value,'reused':False}

    def close(self,worker_id,*,force=False):
        folder=self.folder(worker_id)
        with owner_lock(folder):
            value=self.inspect(worker_id)
            if value['state']!='live':return {'closed':False,'reason':value['state']}
            # Close only this process's browser before terminating its MCP service.
            from .browser_transport import BrowserRPC
            try:
                with BrowserRPC(value['endpoint']) as client:client.call('browser_close',{})
            except Exception:
                if not force:raise
            if process_identity(value['pid'])!=value['process_identity']:
                raise RuntimeError('Browser process ownership changed; no process was stopped.')
            if os.name=='nt':
                from ctypes import wintypes
                kernel=ctypes.WinDLL('kernel32',use_last_error=True)
                kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD];kernel.OpenProcess.restype=wintypes.HANDLE
                kernel.TerminateProcess.argtypes=[wintypes.HANDLE,wintypes.UINT]
                kernel.CloseHandle.argtypes=[wintypes.HANDLE]
                handle=kernel.OpenProcess(1,False,value['pid'])
                if handle:
                    try:kernel.TerminateProcess(handle,0)
                    finally:kernel.CloseHandle(handle)
            else:os.kill(value['pid'],15)
            value['state']='closed';value['closed_at_unix']=time.time()
            (folder/'session.json').write_text(json.dumps(value),encoding='utf-8')
            return {'closed':True,'worker_id':worker_id,'generation':value['generation']}

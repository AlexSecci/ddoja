import json
from pathlib import Path

import pytest

from app import browser_sessions
from app.browser_sessions import BrowserSessions,process_identity


def saved(tmp_path,monkeypatch,identity='same'):
    registry=BrowserSessions(tmp_path);folder=registry.folder('one');folder.mkdir(parents=True)
    (folder/'session.json').write_text(json.dumps({'worker_id':'one','root':str(tmp_path.resolve()),
        'pid':123,'process_identity':'same','port':12345,'generation':'saved','endpoint':'http://localhost:12345/mcp'}))
    monkeypatch.setattr(browser_sessions,'process_identity',lambda pid:identity)
    return registry


def test_reuses_verified_live_process_without_starting_replacement(tmp_path,monkeypatch):
    registry=saved(tmp_path,monkeypatch)
    monkeypatch.setattr(registry,'reachable',lambda value:True)
    monkeypatch.setattr(browser_sessions.subprocess,'Popen',lambda *a,**k:pytest.fail('A live browser cannot be replaced'))
    assert registry.ensure('one',{})['generation']=='saved'
    assert registry.ensure('one',{})['reused']


def test_observation_timeout_of_live_service_is_not_process_loss(tmp_path,monkeypatch):
    registry=saved(tmp_path,monkeypatch)
    monkeypatch.setattr(registry,'reachable',lambda value:False)
    monkeypatch.setattr(browser_sessions.subprocess,'Popen',lambda *a,**k:pytest.fail('Do not restart on timeout'))
    with pytest.raises(RuntimeError,match='live but unavailable'):registry.ensure('one',{})
    assert registry.inspect('one')['state']=='live'


def test_pid_reuse_and_other_owner_cannot_be_claimed(tmp_path,monkeypatch):
    registry=saved(tmp_path,monkeypatch,'different')
    assert registry.inspect('one')['state']=='lost'
    folder=registry.folder('two');folder.mkdir();(folder/'session.json').write_text((registry.folder('one')/'session.json').read_text())
    with pytest.raises(RuntimeError,match='owner metadata'):registry.inspect('two')
    with pytest.raises(ValueError):registry.folder('../one')


def test_current_process_has_stable_actual_creation_identity():
    import os
    assert process_identity(os.getpid()) and process_identity(os.getpid())==process_identity(os.getpid())


def test_lost_browser_after_possible_send_does_not_spawn_replacement(tmp_path,monkeypatch):
    from app import browser_backends
    monkeypatch.setattr(browser_backends,'playwright_status',lambda *_:{'ready':True,'node':'node','cli':'fixture-cli','browser':'fixture-browser'})
    monkeypatch.setattr(BrowserSessions,'inspect',lambda *a:{'state':'lost'})
    monkeypatch.setattr(BrowserSessions,'ensure',lambda *a:pytest.fail('A possible send forbids automatic replacement'))
    with pytest.raises(RuntimeError,match='possible send'):
        browser_backends.playwright_args(tmp_path,{'id':'one','role':'submission','context':{'final_action_may_have_been_sent':True}})

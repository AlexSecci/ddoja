import json

from app import browser_backends


def installed_runtime(tmp_path, monkeypatch):
    (tmp_path / 'config').mkdir()
    cli = tmp_path / 'browser-runtime/node_modules/@playwright/mcp/cli.js'
    cli.parent.mkdir(parents=True); cli.write_text('// fixture')
    browser = tmp_path / 'programs/Google/Chrome/Application/chrome.exe'
    browser.parent.mkdir(parents=True); browser.write_text('fixture')
    node = tmp_path / 'node.exe'; node.write_text('fixture')
    monkeypatch.setenv('PROGRAMFILES', str(tmp_path / 'programs'))
    monkeypatch.setattr(browser_backends.shutil, 'which', lambda command: str(node) if command == 'node' else None)
    return node


def test_path_command_in_portable_config_is_an_installed_runtime(tmp_path, monkeypatch):
    node = installed_runtime(tmp_path, monkeypatch)
    (tmp_path / 'config/runtime.json').write_text(json.dumps({'node': 'node'}))
    status = browser_backends.playwright_status(tmp_path)
    assert status['ready'] and status['node'] == str(node)


def test_private_runtime_override_is_used_for_browser_workers(tmp_path, monkeypatch):
    node = installed_runtime(tmp_path, monkeypatch)
    (tmp_path / 'config/runtime.json').write_text(json.dumps({'node': str(tmp_path / 'absent.exe')}))
    (tmp_path / 'config/runtime.local.json').write_text(json.dumps({'node': str(node)}))
    status = browser_backends.playwright_status(tmp_path)
    assert status['ready'] and status['node'] == str(node)


def test_missing_explicit_runtime_path_still_fails_closed(tmp_path, monkeypatch):
    installed_runtime(tmp_path, monkeypatch)
    (tmp_path / 'config/runtime.json').write_text(json.dumps({'node': str(tmp_path / 'absent.exe')}))
    assert not browser_backends.playwright_status(tmp_path)['ready']

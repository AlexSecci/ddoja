import json

import pytest

from app import rendering
from tools.configure_gmail_broker import configure


def test_private_runtime_override_and_path_expansion(tmp_path, monkeypatch):
    folder = tmp_path / 'config'; folder.mkdir()
    (folder / 'runtime.json').write_text(json.dumps({'node': 'node', 'document_python': '~/default/python'}))
    (folder / 'runtime.local.json').write_text(json.dumps({'document_python': '$DDOJA_RENDER_ROOT/python.exe'}))
    monkeypatch.setattr(rendering, 'CODE_ROOT', tmp_path)
    monkeypatch.setenv('DDOJA_RENDER_ROOT', str(tmp_path / 'private'))
    config = rendering.runtime_config()
    assert config['document_python'] == str(tmp_path / 'private') + '/python.exe'
    assert config['node'] == 'node'
    assert 'private' not in (folder / 'runtime.json').read_text()


def test_gmail_setup_requires_explicit_account_before_accessing_client_or_browser(tmp_path):
    with pytest.raises(ValueError, match='expected Gmail account'):
        configure(tmp_path / 'nonexistent-private-client.json')

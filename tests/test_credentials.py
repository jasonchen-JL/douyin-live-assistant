import json

import pytest
from fastapi.testclient import TestClient

from assistant_app import credentials
from assistant_app.api import create_app
from assistant_app.config import default_model
from assistant_app.store import Store


def configured(secret='fixture-secret-only'):
    return {**default_model('screening').model_dump(), 'base_url': 'https://test.invalid/v1',
            'model': 'fixture', 'api_key': secret}


def test_secret_not_in_sqlite_or_public_response(tmp_path):
    path = tmp_path / 'assistant.sqlite3'
    store = Store(path)
    result = store.save_model('screening', configured())
    assert result['key_configured']
    assert 'api_key' not in result and 'credential_ref' not in result
    assert b'fixture-secret-only' not in path.read_bytes()
    with store.connect() as db:
        bodies = [r[0] for r in db.execute('SELECT body FROM models')]
        assert all('fixture-secret-only' not in b for b in bodies)
        assert json.loads(bodies[-1])['credential_ref']
    reopened = Store(path)
    assert reopened.model('screening')['api_key'] == 'fixture-secret-only'


def test_versions_and_roles_keep_independent_keys(tmp_path):
    store = Store(tmp_path / 'assistant.sqlite3')
    original = store.save_model('screening', configured())['version']
    store.save_model('analysis', configured('other-fixture-secret'))
    store.save_model('screening', configured('changed-fixture-secret'))
    store.save_model('screening', configured(''))
    assert store.model('screening')['api_key'] == 'changed-fixture-secret'
    store.save_model('screening', {**configured(''), 'clear_key': True})
    assert store.model('screening')['api_key'] == ''
    assert store.model('screening', original)['api_key'] == 'fixture-secret-only'
    assert store.model('analysis')['api_key'] == 'other-fixture-secret'


def test_failed_vault_save_does_not_commit_config(tmp_path, monkeypatch):
    app = create_app(tmp_path, background=False)
    before = app.state.store.model('screening')['version']

    class BrokenVault:
        def set_password(self, *args):
            raise RuntimeError('fixture-secret-must-not-leak')

    monkeypatch.setattr(credentials, 'backend', BrokenVault)
    response = TestClient(app).put('/api/settings/models/screening', json=configured(),
                                   headers={'x-live-assistant': '1'})
    assert response.status_code == 503
    assert response.json() == {'error': 'credential_save_failed'}
    assert app.state.store.model('screening')['version'] == before


def test_missing_vault_entry_fails_closed(tmp_path, credential_vault):
    store = Store(tmp_path / 'assistant.sqlite3')
    store.save_model('screening', configured())
    credential_vault.clear()
    with pytest.raises(credentials.CredentialError, match='credential_not_found'):
        store.model('screening')
    assert store.model('screening', public=True)['key_configured']

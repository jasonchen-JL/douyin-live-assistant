import pytest

from assistant_app.store import Store
from .support import configure


@pytest.fixture(autouse=True)
def credential_vault(monkeypatch):
    from assistant_app import credentials

    entries = {}

    class MemoryVault:
        def set_password(self, service, reference, secret):
            entries[(service, reference)] = secret

        def get_password(self, service, reference):
            return entries.get((service, reference))

    monkeypatch.setattr(credentials, 'backend', MemoryVault)
    return entries


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / 'assistant.sqlite3')
    configure(value)
    return value

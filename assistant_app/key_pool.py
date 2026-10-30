"""One shared account budget across roles; no plaintext credentials in SQLite."""
import asyncio
import hashlib
import json
import time
import uuid

from . import credentials
from .config import PoolEntrySettings


class KeyPool:
    def __init__(self, store):
        self.store = store
        self.active = {}
        self.used = {}

    @staticmethod
    def initialize(db):
        db.executescript('''
            CREATE TABLE IF NOT EXISTS key_pool (id TEXT PRIMARY KEY, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS key_pool_health (
                rate_key TEXT PRIMARY KEY, next_request_at REAL NOT NULL DEFAULT 0,
                cooldown_until REAL NOT NULL DEFAULT 0, blocked_reason TEXT);
            CREATE TABLE IF NOT EXISTS job_attempt_routes (
                job_id TEXT, attempt TEXT, entry_id TEXT, base_url TEXT, credential_ref TEXT,
                started_at REAL, PRIMARY KEY(job_id,attempt));
        ''')

    def entries(self, public=True):
        with self.store.connect() as db:
            entries = [{'id': r['id'], **json.loads(r['body'])} for r in db.execute('SELECT * FROM key_pool ORDER BY id')]
            if public:
                for entry in entries:
                    rate_key = entry.pop('rate_key')
                    entry['key_configured'] = bool(entry.pop('credential_ref', ''))
                    health = db.execute('SELECT * FROM key_pool_health WHERE rate_key=?', (rate_key,)).fetchone()
                    entry.update(active=self.active.get(rate_key, 0),
                                 cooldown_until=health['cooldown_until'] if health else 0,
                                 blocked_reason=health['blocked_reason'] if health else None)
            return entries

    def save(self, entry_id, value):
        value = PoolEntrySettings.model_validate(value).model_dump()
        with self.store.connect(True) as db:
            row = db.execute('SELECT body FROM key_pool WHERE id=?', (entry_id,)).fetchone()
            old = json.loads(row[0]) if row else {}
            secret = value.pop('api_key')
            if secret:
                value['credential_ref'] = credentials.save_key(secret)
                value['rate_key'] = hashlib.sha256(secret.encode()).hexdigest()
            elif old:
                value.update({k: old[k] for k in ('credential_ref', 'rate_key')})
            else:
                raise credentials.CredentialError('pool_key_required')
            entry_id = entry_id or uuid.uuid4().hex
            db.execute('INSERT INTO key_pool VALUES(?,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body',
                       (entry_id, json.dumps(value)))
        return next(e for e in self.entries() if e['id'] == entry_id)

    def reset(self, entry_id):
        entry = next((e for e in self.entries(False) if e['id'] == entry_id), None)
        if not entry:
            raise ValueError('pool_entry_not_found')
        with self.store.connect(True) as db:
            # Manual auth reset must not bypass an outstanding server rate-limit cooldown.
            db.execute('UPDATE key_pool_health SET blocked_reason=NULL WHERE rate_key=?', (entry['rate_key'],))

    def routes(self, ids):
        entries = {e['id']: e for e in self.entries(False)}
        if any(i not in entries for i in ids):
            raise ValueError('pool_entry_not_found')
        return [entries[i] for i in ids]

    def acquire(self, routes):
        now = time.time()
        current = {e['id']: e for e in self.entries(False)}
        candidates = [route if route.get('legacy') else current.get(route['id']) for route in routes]
        ordered = sorted((route for route in candidates if route and route['enabled']),
                         key=lambda route: self.used.get(route['rate_key'], 0))
        with self.store.connect() as db:
            for route in ordered:
                key = route['rate_key']
                siblings = [e for e in current.values() if e['rate_key'] == key]
                cap = min([route['max_concurrency']] + [e['max_concurrency'] for e in siblings])
                interval = max([route['min_interval_seconds']] + [e['min_interval_seconds'] for e in siblings])
                health = db.execute('SELECT * FROM key_pool_health WHERE rate_key=?', (key,)).fetchone()
                if (self.active.get(key, 0) >= cap or (health and (
                        health['blocked_reason'] or max(health['cooldown_until'], health['next_request_at']) > now))):
                    continue
                self.active[key] = self.active.get(key, 0) + 1
                self.used[key] = time.monotonic()
                return {**route, 'min_interval_seconds': interval}
        return None

    async def before_request(self, route):
        # Long-history compression also makes HTTP calls and must use the same account budget.
        key = route['rate_key']
        while True:
            with self.store.connect(True) as db:
                db.execute('INSERT OR IGNORE INTO key_pool_health(rate_key) VALUES(?)', (key,))
                row = db.execute('SELECT * FROM key_pool_health WHERE rate_key=?', (key,)).fetchone()
                if row['blocked_reason']:
                    raise credentials.CredentialError('pool_account_blocked')
                now = time.time()
                delay = max(row['next_request_at'], row['cooldown_until']) - now
                if delay <= 0:
                    db.execute('UPDATE key_pool_health SET next_request_at=? WHERE rate_key=?',
                               (now + route['min_interval_seconds'], key))
                    return
            await asyncio.sleep(min(delay, 1))

    def release(self, route, error=None):
        key = route['rate_key']
        self.active[key] = max(0, self.active.get(key, 1) - 1)
        code = getattr(error, 'code', '')
        if code in ('model_http_401', 'model_http_403', 'model_http_429'):
            with self.store.connect(True) as db:
                db.execute('INSERT OR IGNORE INTO key_pool_health(rate_key) VALUES(?)', (key,))
                if code == 'model_http_429':
                    db.execute('UPDATE key_pool_health SET cooldown_until=MAX(cooldown_until,?) WHERE rate_key=?',
                               (time.time() + max(60, getattr(error, 'retry_after', 0)), key))
                else:
                    db.execute('UPDATE key_pool_health SET blocked_reason=? WHERE rate_key=?', (code, key))

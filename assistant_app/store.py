import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .config import CHINA, GeneralSettings, default_model
from . import credentials
from .key_pool import KeyPool
from .knowledge import DEFAULT_KNOWLEDGE_PATH, snapshot as knowledge_snapshot


def dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(dump(value).encode()).hexdigest()


def timestamp(event):
    raw = event.get('source_time_raw')
    try:
        if event.get('source_time_kind') in ('unix_seconds', 'payload.event_time_unix_seconds') and raw:
            result = datetime.fromtimestamp(float(raw), timezone.utc)
            if result.year < 2000 or result.year > 2100:
                raise ValueError('Invalid platform time')
            return result
    except (ValueError, OverflowError, OSError):
        pass
    result = datetime.fromisoformat(event['captured_at'].replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('Missing observation timezone')
    return result


class Store:
    def __init__(self, path, knowledge_path=DEFAULT_KNOWLEDGE_PATH):
        self.path = Path(path)
        self.knowledge_path = knowledge_path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS models (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, role TEXT NOT NULL, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS source_seen (
                    source TEXT, event_id TEXT, error TEXT, PRIMARY KEY(source,event_id));
                CREATE TABLE IF NOT EXISTS comments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT UNIQUE NOT NULL,
                    room TEXT NOT NULL, uid TEXT NOT NULL, day TEXT NOT NULL,
                    nickname TEXT NOT NULL, content TEXT NOT NULL, sent_at REAL NOT NULL,
                    platform_message_id TEXT NOT NULL, platform_room_id TEXT NOT NULL,
                    raw TEXT NOT NULL, background TEXT NOT NULL,
                    screening_status TEXT NOT NULL DEFAULT 'pending', job_id TEXT,
                    UNIQUE(room,platform_message_id));
                CREATE INDEX IF NOT EXISTS comments_viewer ON comments(room,uid,day,id);
                CREATE INDEX IF NOT EXISTS comments_time ON comments(room,day,sent_at,id);
                CREATE INDEX IF NOT EXISTS comments_pending ON comments(screening_status,id);
                CREATE TABLE IF NOT EXISTS viewers (
                    room TEXT, uid TEXT, day TEXT, latest_revision INTEGER NOT NULL DEFAULT 0,
                    required_gen INTEGER NOT NULL DEFAULT 0, settled_gen INTEGER NOT NULL DEFAULT 0,
                    published_gen INTEGER NOT NULL DEFAULT 0, running_job TEXT,
                    result_id TEXT, error TEXT, PRIMARY KEY(room,uid,day));
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, role TEXT NOT NULL, status TEXT NOT NULL,
                    attempt TEXT, attempts INTEGER NOT NULL DEFAULT 0,
                    retry_at REAL NOT NULL DEFAULT 0, created_at REAL NOT NULL,
                    config_id INTEGER NOT NULL, payload TEXT NOT NULL,
                    result TEXT, error TEXT, finished_at REAL);
                CREATE INDEX IF NOT EXISTS jobs_queue ON jobs(role,status,retry_at,created_at);
                CREATE TABLE IF NOT EXISTS results (
                    id TEXT PRIMARY KEY, job_id TEXT NOT NULL, room TEXT, uid TEXT, day TEXT,
                    generation INTEGER NOT NULL, input_hash TEXT NOT NULL,
                    coverage TEXT NOT NULL, body TEXT NOT NULL, created_at REAL NOT NULL,
                    config_id INTEGER NOT NULL, UNIQUE(job_id,room,uid,day));
                CREATE INDEX IF NOT EXISTS results_input ON results(input_hash,config_id);
                CREATE TABLE IF NOT EXISTS analysis_requests (
                    request_id TEXT PRIMARY KEY, room TEXT NOT NULL, uid TEXT NOT NULL,
                    created_at REAL NOT NULL, job_id TEXT);
                CREATE INDEX IF NOT EXISTS analysis_requests_viewer ON analysis_requests(room,uid,job_id);
                CREATE INDEX IF NOT EXISTS comments_preview ON comments(room,uid,sent_at DESC,id DESC);
            ''')
            KeyPool.initialize(db)
            columns = {r['name'] for r in db.execute('PRAGMA table_info(jobs)')}
            if 'routes' not in columns:
                db.execute('ALTER TABLE jobs ADD COLUMN routes TEXT')
            if 'pool_entry_id' not in columns:
                db.execute('ALTER TABLE jobs ADD COLUMN pool_entry_id TEXT')
            if not db.execute('SELECT 1 FROM settings').fetchone():
                db.execute('INSERT INTO settings VALUES(1,?)', (dump(GeneralSettings().model_dump()),))
            db.execute('CREATE TABLE IF NOT EXISTS app_migrations (name TEXT PRIMARY KEY)')
            if not db.execute("SELECT 1 FROM app_migrations WHERE name='markdown-direct-v1'").fetchone():
                settings = json.loads(db.execute('SELECT body FROM settings WHERE id=1').fetchone()[0])
                settings['analysis_mode'] = 'markdown_direct'
                db.execute('UPDATE settings SET body=? WHERE id=1', (dump(settings),))
                db.execute("INSERT INTO app_migrations VALUES('markdown-direct-v1')")
            for role in ('screening', 'analysis'):
                if not db.execute('SELECT 1 FROM models WHERE role=?', (role,)).fetchone():
                    db.execute('INSERT INTO models(role,body) VALUES(?,?)',
                               (role, dump(default_model(role).model_dump(exclude={'clear_key'}))))
        self.pool = KeyPool(self)

    @contextmanager
    def connect(self, write=False):
        db = sqlite3.connect(self.path, timeout=5, uri=True)
        db.row_factory = sqlite3.Row
        try:
            if write:
                db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def settings(self):
        with self.connect() as db:
            return {**GeneralSettings().model_dump(), **json.loads(db.execute('SELECT body FROM settings WHERE id=1').fetchone()[0])}

    def save_settings(self, value):
        with self.connect(True) as db:
            db.execute('UPDATE settings SET body=? WHERE id=1', (dump(value),))

    def model(self, role, config_id=None, public=False):
        with self.connect() as db:
            row = db.execute('SELECT * FROM models WHERE id=?', (config_id,)).fetchone() if config_id else db.execute(
                'SELECT * FROM models WHERE role=? ORDER BY id DESC LIMIT 1', (role,)).fetchone()
            value = json.loads(row['body'])
            value.setdefault('pool_ids', [])
            reference = value.pop('credential_ref', '')
            value['version'] = row['id']
            value['configured'] = bool((value['pool_ids'] or value['base_url']) and value['model'])
            if public:
                value['key_configured'] = bool(reference or value.pop('api_key', ''))
                value.pop('api_key', None)
            elif reference and not value['pool_ids']:
                value['api_key'] = credentials.read_key(reference)
            else:
                value.setdefault('api_key', '')
            return value

    def save_model(self, role, value):
        if value.get('pool_ids'):
            self.pool.routes(value['pool_ids'])
        with self.connect(True) as db:
            current = json.loads(db.execute('SELECT body FROM models WHERE role=? ORDER BY id DESC LIMIT 1', (role,)).fetchone()[0])
            value = dict(value)
            clear = value.pop('clear_key', False)
            secret = value.pop('api_key', '')
            value.pop('credential_ref', None)
            if clear:
                reference = ''
            elif secret:
                reference = credentials.save_key(secret)
            else:
                reference = current.get('credential_ref', '')
                if not reference and current.get('api_key'):
                    reference = credentials.save_key(current['api_key'])
            value['credential_ref'] = reference
            db.execute('INSERT INTO models(role,body) VALUES(?,?)', (role, dump(value)))
        return self.model(role, public=True)

    def import_source(self, path, limit=1000):
        path = Path(path)
        if not path.exists():
            return 0
        if path.resolve() == self.path.resolve():
            raise ValueError('Source and application database must differ')
        settings = self.settings()
        background = {k: settings[k] for k in ('topic', 'framework', 'company_capabilities')}
        source = str(path.resolve())
        with self.connect(True) as db:
            db.execute('ATTACH DATABASE ? AS source_db', (path.resolve().as_uri() + '?mode=ro',))
            rows = db.execute('''SELECT c.* FROM source_db.comments c WHERE NOT EXISTS
                (SELECT 1 FROM source_seen s WHERE s.source=? AND s.event_id=c.event_id)
                ORDER BY c.rowid LIMIT ?''', (source, limit)).fetchall()
            added = 0
            for row in rows:
                event = dict(row)
                error = None
                try:
                    added += self._insert(db, event, background)
                except (ValueError, KeyError, TypeError):
                    error = 'invalid_comment_identity_or_time'
                db.execute('INSERT OR IGNORE INTO source_seen VALUES(?,?,?)', (source, event['event_id'], error))
            return added

    def ingest(self, events):
        """Used by isolated fixtures and future authorized comment adapters."""
        settings = self.settings()
        with self.connect(True) as db:
            return sum(self._insert(db, event, {k: settings[k] for k in ('topic', 'framework', 'company_capabilities')}) for event in events)

    def _insert(self, db, event, background):
        for name in ('event_id', 'room_url', 'user_id', 'platform_message_id', 'platform_room_id'):
            if not isinstance(event.get(name), str) or not event[name]:
                raise ValueError('Missing identity')
        for name in ('nickname', 'content', 'captured_at'):
            if not isinstance(event.get(name), str):
                raise ValueError('Invalid comment text')
        moment = timestamp(event)
        room, uid, day = event['room_url'], event['user_id'], moment.astimezone(CHINA).date().isoformat()
        row = db.execute('''INSERT INTO comments
            (event_id,room,uid,day,nickname,content,sent_at,platform_message_id,platform_room_id,raw,background)
            VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING RETURNING id''',
            (event['event_id'], room, uid, day, event['nickname'], event['content'], moment.timestamp(),
             event['platform_message_id'], event['platform_room_id'], dump(event), dump(background))).fetchone()
        if row:
            db.execute('''INSERT INTO viewers(room,uid,day,latest_revision) VALUES(?,?,?,?)
                ON CONFLICT(room,uid,day) DO UPDATE SET latest_revision=excluded.latest_revision''', (room, uid, day, row[0]))
        return int(row is not None)

    @staticmethod
    def comment(row):
        raw = json.loads(row['raw'])
        return {**{k: row[k] for k in ('id', 'event_id', 'nickname', 'content', 'sent_at', 'platform_message_id', 'platform_room_id')},
                'captured_at': raw['captured_at'], 'source_time_raw': raw.get('source_time_raw'),
                'source_time_kind': raw.get('source_time_kind'), 'background_at_import': json.loads(row['background'])}

    def recover(self):
        # The process-wide OS lock must be held before recovering interrupted jobs.
        with self.connect(True) as db:
            db.execute("UPDATE jobs SET status='pending',attempt=NULL,retry_at=0 WHERE status='running'")

    def claim(self, role):
        config = self.model(role, public=True)
        general = self.settings()
        if not general['analysis_enabled'] or not config['configured']:
            return None
        markdown = general['analysis_mode'] == 'markdown_direct'
        if markdown and role == 'screening':
            return None
        with self.connect(True) as db:
            if markdown:
                self._retire_legacy_jobs(db)
            retry = db.execute("SELECT * FROM jobs WHERE role=? AND status='pending' AND retry_at<=? ORDER BY created_at LIMIT 1",
                               (role, time.time())).fetchone()
            if retry:
                job_id = retry['id']
            else:
                if role == 'screening' and general['analysis_mode'] == 'direct':
                    return None
                if not config.get('pool_ids') and db.execute("SELECT 1 FROM jobs WHERE role=? AND config_id=? AND status='failed' AND error IN ('model_http_401','model_http_403') LIMIT 1", (role, config['version'])).fetchone():
                    return None
                payload = (self._markdown_payload(db, general) if markdown else
                           self._screening_payload(db, general) if role == 'screening' else self._analysis_payload(db, general))
                if role == 'analysis' and not markdown:
                    self._reuse_analysis(db, payload, config['version'])
                    if not payload['viewers'] and general['analysis_mode'] == 'direct':
                        payload = self._direct_payload(db, general)
                if not payload['viewers']:
                    return None
                job_id = uuid.uuid4().hex
                db.execute('INSERT INTO jobs(id,role,status,created_at,config_id,payload) VALUES(?,?,?,?,?,?)',
                           (job_id, role, 'pending', time.time(), config['version'], dump(payload)))
                if payload.get('trigger') == 'manual':
                    db.execute('UPDATE analysis_requests SET job_id=? WHERE room=? AND uid=? AND job_id IS NULL',
                               (job_id, payload['room'], payload['viewers'][0]['user_id']))
                for viewer in payload['viewers']:
                    if role == 'screening':
                        for event_id in viewer['target_event_ids']:
                            db.execute("UPDATE comments SET screening_status='processing',job_id=? WHERE event_id=? AND screening_status='pending'", (job_id, event_id))
                    else:
                        db.execute('UPDATE viewers SET running_job=?,error=NULL WHERE room=? AND uid=? AND day=?',
                                   (job_id, payload['room'], viewer['user_id'], payload['day']))
                        if payload.get('pipeline_mode') in ('direct', 'markdown_direct'):
                            db.execute('UPDATE viewers SET required_gen=? WHERE room=? AND uid=? AND day=?',
                                       (viewer['generation'], payload['room'], viewer['user_id'], payload['day']))
                            for event_id in viewer['target_event_ids']:
                                db.execute("UPDATE comments SET screening_status='processing',job_id=? WHERE event_id=? AND screening_status IN ('pending','failed')", (job_id, event_id))
            stored = db.execute('SELECT config_id,routes FROM jobs WHERE id=?', (job_id,)).fetchone()
            if not stored['routes']:
                frozen = json.loads(db.execute('SELECT body FROM models WHERE id=?', (stored['config_id'],)).fetchone()[0])
                if frozen.get('pool_ids'):
                    routes = self.pool.routes(frozen['pool_ids'])
                else:
                    reference = frozen.get('credential_ref', '')
                    secret = self.model(role, stored['config_id']).get('api_key', '')
                    routes = [{'id': 'legacy-' + role, 'legacy': True, 'enabled': True,
                               'base_url': frozen['base_url'], 'credential_ref': reference,
                               'rate_key': hashlib.sha256((secret or frozen['base_url']).encode()).hexdigest(),
                               'max_concurrency': 1, 'min_interval_seconds': 0}]
                db.execute('UPDATE jobs SET routes=? WHERE id=?', (dump(routes), job_id))
            attempt = uuid.uuid4().hex
            db.execute("UPDATE jobs SET status='running',attempt=?,attempts=attempts+1,error=NULL WHERE id=?", (attempt, job_id))
            row = dict(db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone())
            row['payload'] = json.loads(row['payload'])
            row['routes'] = json.loads(row['routes'])
            return row

    @staticmethod
    def _retire_legacy_jobs(db):
        # Keep the original payload/result for rollback/audit; never resume old JSON retries.
        legacy = db.execute("""SELECT payload FROM jobs WHERE role='analysis' AND status IN ('pending','failed')
            AND COALESCE(json_extract(payload,'$.pipeline_mode'),'')!='markdown_direct'""").fetchall()
        for row in legacy:
            payload = json.loads(row['payload'])
            for viewer in payload['viewers']:
                state = db.execute('SELECT published_gen FROM viewers WHERE room=? AND uid=? AND day=?',
                                   (payload['room'], viewer['user_id'], payload['day'])).fetchone()
                if state and state['published_gen'] < viewer['generation']:
                    for comment in viewer['comments']:
                        db.execute("UPDATE comments SET screening_status='pending',job_id=NULL WHERE event_id=? AND screening_status='done'",
                                   (comment['event_id'],))
        db.execute("""UPDATE jobs SET status='superseded',attempt=NULL,error='markdown_mode_replaced',finished_at=?
            WHERE status IN ('pending','failed') AND COALESCE(json_extract(payload,'$.pipeline_mode'),'')!='markdown_direct'""", (time.time(),))
        db.execute("""UPDATE comments SET screening_status='pending',job_id=NULL WHERE screening_status IN ('processing','failed')
            AND job_id IN (SELECT id FROM jobs WHERE status='superseded' AND error='markdown_mode_replaced')""")
        db.execute("""UPDATE viewers SET running_job=NULL WHERE running_job IN
            (SELECT id FROM jobs WHERE status='superseded' AND error='markdown_mode_replaced')""")
        # Old screening may already have marked comments done but left analysis queued.
        db.execute("""UPDATE comments SET screening_status='pending',job_id=NULL WHERE screening_status='done'
            AND EXISTS(SELECT 1 FROM viewers v WHERE v.room=comments.room AND v.uid=comments.uid
                AND v.day=comments.day AND v.required_gen>v.settled_gen AND v.running_job IS NULL)""")
        db.execute('UPDATE viewers SET settled_gen=required_gen WHERE required_gen>settled_gen AND running_job IS NULL')

    def _markdown_payload(self, db, settings):
        manual = db.execute('''SELECT r.room,r.uid FROM analysis_requests r WHERE r.job_id IS NULL
            AND NOT EXISTS(SELECT 1 FROM viewers v WHERE v.room=r.room AND v.uid=r.uid AND v.running_job IS NOT NULL)
            AND NOT EXISTS(SELECT 1 FROM comments c WHERE c.room=r.room AND c.uid=r.uid AND c.screening_status='processing')
            ORDER BY r.created_at,r.request_id LIMIT 1''').fetchone()
        first = manual or db.execute("""SELECT c.room,c.uid FROM comments c WHERE c.screening_status='pending'
            AND NOT EXISTS(SELECT 1 FROM viewers v WHERE v.room=c.room AND v.uid=c.uid AND v.running_job IS NOT NULL)
            AND NOT EXISTS(SELECT 1 FROM comments p WHERE p.room=c.room AND p.uid=c.uid AND p.screening_status='processing')
            ORDER BY c.id LIMIT 1""").fetchone()
        if not first:
            return {'viewers': []}
        rows = db.execute('SELECT * FROM comments WHERE room=? AND uid=? ORDER BY sent_at,id', tuple(first)).fetchall()
        day = max(r['day'] for r in rows)
        state = db.execute('SELECT * FROM viewers WHERE room=? AND uid=? AND day=?', (*first, day)).fetchone()
        viewer = {'user_id': first['uid'], 'nickname': max(rows, key=lambda r: r['id'])['nickname'],
                  'generation': state['required_gen'] + 1, 'input_revision': max(r['id'] for r in rows),
                  'target_event_ids': [r['event_id'] for r in rows if r['screening_status'] in ('pending', 'failed')],
                  'comments': [self.comment(r) for r in rows]}
        return {**self._payload(settings, first['room'], day, [viewer]),
                'pipeline_mode': 'markdown_direct', 'history_scope': 'all_collected_for_room_and_uid',
                'trigger': 'manual' if manual else 'comments',
                'knowledge': knowledge_snapshot(self.knowledge_path)}

    def request_analysis(self, room, uid, request_id):
        with self.connect(True) as db:
            existing = db.execute('''SELECT r.*,j.status FROM analysis_requests r
                LEFT JOIN jobs j ON j.id=r.job_id WHERE r.request_id=?''', (request_id,)).fetchone()
            if existing:
                if (existing['room'], existing['uid']) != (room, uid):
                    raise ValueError('analysis_request_conflict')
                return {'request_id': request_id, 'job_id': existing['job_id'],
                        'state': existing['status'] or 'queued'}
            settings = json.loads(db.execute('SELECT body FROM settings WHERE id=1').fetchone()[0])
            if settings['room_url'] != room:
                raise ValueError('analysis_room_changed')
            if settings.get('analysis_mode') != 'markdown_direct':
                raise ValueError('manual_analysis_requires_markdown')
            if not settings['analysis_enabled']:
                raise ValueError('analysis_paused')
            config = json.loads(db.execute("SELECT body FROM models WHERE role='analysis' ORDER BY id DESC LIMIT 1").fetchone()[0])
            if not config.get('model') or not (config.get('pool_ids') or config.get('base_url')):
                raise ValueError('analysis_model_not_configured')
            if not db.execute('SELECT 1 FROM comments WHERE room=? AND uid=? LIMIT 1', (room, uid)).fetchone():
                raise ValueError('viewer_not_found')
            db.execute('INSERT INTO analysis_requests(request_id,room,uid,created_at) VALUES(?,?,?,?)',
                       (request_id, room, uid, time.time()))
            return {'request_id': request_id, 'job_id': None, 'state': 'queued'}

    def _screening_payload(self, db, settings):
        first = db.execute("""SELECT room,day,uid FROM comments c WHERE screening_status='pending'
            AND NOT EXISTS(SELECT 1 FROM comments p WHERE p.room=c.room AND p.uid=c.uid AND p.day=c.day
                AND p.screening_status='processing') ORDER BY id LIMIT 1""").fetchone()
        if not first:
            return {'viewers': []}
        rows = db.execute("SELECT * FROM comments WHERE room=? AND day=? AND uid=? AND screening_status='pending' ORDER BY id LIMIT ?",
                          (*first, settings['screening_batch_limit'])).fetchall()
        grouped = {}
        for row in rows:
            uid = row['uid']
            if uid not in grouped and len(grouped) >= settings['screening_viewer_limit']:
                break
            grouped.setdefault(uid, []).append(row)
        viewers = []
        for uid, targets in grouped.items():
            history = db.execute('SELECT * FROM comments WHERE room=? AND day=? AND uid=? AND id<? ORDER BY id DESC LIMIT ?',
                                 (first['room'], first['day'], uid, targets[0]['id'], settings['screening_history_comment_limit'])).fetchall()
            viewers.append({'user_id': uid, 'nickname': targets[-1]['nickname'],
                            'target_event_ids': [r['event_id'] for r in targets],
                            'new_comments': [self.comment(r) for r in sorted(targets, key=lambda r: (r['sent_at'], r['id']))],
                            'history_comments': [self.comment(r) for r in sorted(history, key=lambda r: (r['sent_at'], r['id']))]})
        return self._payload(settings, first['room'], first['day'], viewers)

    def _analysis_payload(self, db, settings):
        first = db.execute('''SELECT room,day FROM viewers WHERE required_gen>settled_gen
            AND running_job IS NULL ORDER BY latest_revision LIMIT 1''').fetchone()
        if not first:
            return {'viewers': []}
        states = db.execute('''SELECT * FROM viewers WHERE room=? AND day=? AND required_gen>settled_gen
            AND running_job IS NULL ORDER BY latest_revision LIMIT 1''', tuple(first)).fetchall()
        viewers = []
        for state in states:
            rows = db.execute('SELECT * FROM comments WHERE room=? AND uid=? AND day=? ORDER BY sent_at,id',
                              (state['room'], state['uid'], state['day'])).fetchall()
            viewers.append({'user_id': state['uid'], 'nickname': max(rows, key=lambda r: r['id'])['nickname'],
                            'generation': state['required_gen'], 'input_revision': state['latest_revision'],
                            'comments': [self.comment(r) for r in rows]})
        return self._payload(settings, first['room'], first['day'], viewers)

    def _direct_payload(self, db, settings):
        first = db.execute('''SELECT v.* FROM viewers v JOIN comments c ON c.room=v.room AND c.uid=v.uid AND c.day=v.day
            WHERE c.screening_status='pending' AND v.running_job IS NULL
            AND NOT EXISTS(SELECT 1 FROM comments p WHERE p.room=v.room AND p.uid=v.uid AND p.day=v.day
                AND p.screening_status='processing') ORDER BY c.id LIMIT 1''').fetchone()
        if not first:
            return {'viewers': []}
        rows = db.execute('SELECT * FROM comments WHERE room=? AND uid=? AND day=? ORDER BY sent_at,id',
                          (first['room'], first['uid'], first['day'])).fetchall()
        viewer = {'user_id': first['uid'], 'nickname': max(rows, key=lambda r: r['id'])['nickname'],
                  'generation': first['required_gen'] + 1, 'input_revision': first['latest_revision'],
                  'target_event_ids': [r['event_id'] for r in rows if r['screening_status'] == 'pending'],
                  'comments': [self.comment(r) for r in rows]}
        return {**self._payload(settings, first['room'], first['day'], [viewer]), 'pipeline_mode': 'direct'}

    def defer(self, job, delay=1):
        with self.connect(True) as db:
            db.execute("UPDATE jobs SET status='pending',attempts=MAX(0,attempts-1),retry_at=? WHERE id=? AND status='running' AND attempt=?",
                       (time.time() + delay, job['id'], job['attempt']))

    def set_job_route(self, job, route):
        with self.connect(True) as db:
            db.execute('UPDATE jobs SET pool_entry_id=? WHERE id=? AND attempt=?', (route['id'], job['id'], job['attempt']))
            db.execute('INSERT OR REPLACE INTO job_attempt_routes VALUES(?,?,?,?,?,?)',
                       (job['id'], job['attempt'], route['id'], route['base_url'], route['credential_ref'], time.time()))

    @staticmethod
    def _payload(settings, room, day, viewers):
        return {'room': room, 'day': day, 'timezone': 'Asia/Shanghai', 'rules_version': 'br-v1',
                'background': {k: settings[k] for k in ('topic', 'framework', 'company_capabilities')},
                'speech_context': {'status': 'not_enabled', 'segments': []}, 'viewers': viewers}

    @staticmethod
    def input_hash(payload, viewer):
        return digest({**{k: v for k, v in payload.items() if k != 'viewers'},
                       'viewer': {k: v for k, v in viewer.items() if k not in ('generation', 'input_revision')}})

    def _reuse_analysis(self, db, payload, config_id):
        pending = []
        for viewer in payload['viewers']:
            row = db.execute('SELECT id FROM results WHERE input_hash=? AND config_id=? ORDER BY created_at DESC LIMIT 1',
                             (self.input_hash(payload, viewer), config_id)).fetchone()
            if row:
                db.execute('''UPDATE viewers SET published_gen=?,settled_gen=?,result_id=?,error=NULL
                    WHERE room=? AND uid=? AND day=? AND running_job IS NULL''',
                           (viewer['generation'], viewer['generation'], row['id'], payload['room'], viewer['user_id'], payload['day']))
            else:
                pending.append(viewer)
        payload['viewers'] = pending

    def complete(self, job, results):
        payload = job['payload']
        with self.connect(True) as db:
            current = db.execute('SELECT status,attempt FROM jobs WHERE id=?', (job['id'],)).fetchone()
            if not current or current['status'] != 'running' or current['attempt'] != job['attempt']:
                return False
            for viewer, result in zip(payload['viewers'], results, strict=True):
                key = (payload['room'], viewer['user_id'], payload['day'])
                if job['role'] == 'screening':
                    for event_id in viewer['target_event_ids']:
                        db.execute("UPDATE comments SET screening_status='done' WHERE event_id=? AND job_id=?", (event_id, job['id']))
                    if result['decision'] != 'skip' or result['needs_reply']:
                        db.execute('UPDATE viewers SET required_gen=required_gen+1,error=NULL WHERE room=? AND uid=? AND day=?', key)
                else:
                    if payload.get('pipeline_mode') in ('direct', 'markdown_direct'):
                        for event_id in viewer['target_event_ids']:
                            db.execute("UPDATE comments SET screening_status='done' WHERE event_id=? AND job_id=?", (event_id, job['id']))
                    result_id = uuid.uuid4().hex
                    coverage = [c['event_id'] for c in viewer['comments']]
                    db.execute('INSERT INTO results VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                               (result_id, job['id'], *key, viewer['generation'], self.input_hash(payload, viewer), dump(coverage), dump(result), time.time(), job['config_id']))
                    db.execute('''UPDATE viewers SET published_gen=?,result_id=?,error=NULL
                        WHERE room=? AND uid=? AND day=? AND published_gen<?''',
                               (viewer['generation'], result_id, *key, viewer['generation']))
                    db.execute('''UPDATE viewers SET settled_gen=MAX(settled_gen,?),running_job=NULL
                        WHERE room=? AND uid=? AND day=? AND running_job=?''', (viewer['generation'], *key, job['id']))
            db.execute("UPDATE jobs SET status='done',result=?,finished_at=?,error=NULL WHERE id=?", (dump(results), time.time(), job['id']))
            return True

    def fail(self, job, error, retryable=True):
        terminal = not retryable or job['attempts'] >= 3
        with self.connect(True) as db:
            updated = db.execute("UPDATE jobs SET status=?,error=?,retry_at=? WHERE id=? AND status='running' AND attempt=?",
                                 ('failed' if terminal else 'pending', error, time.time() + 2 ** job['attempts'], job['id'], job['attempt'])).rowcount
            if not updated or not terminal:
                return
            if job['role'] == 'screening':
                db.execute("UPDATE comments SET screening_status='failed' WHERE job_id=? AND screening_status='processing'", (job['id'],))
            else:
                if job['payload'].get('pipeline_mode') in ('direct', 'markdown_direct'):
                    db.execute("UPDATE comments SET screening_status='failed' WHERE job_id=? AND screening_status='processing'", (job['id'],))
                for viewer in job['payload']['viewers']:
                    db.execute('''UPDATE viewers SET running_job=NULL,settled_gen=MAX(settled_gen,?),error=?
                        WHERE room=? AND uid=? AND day=? AND running_job=?''',
                               (viewer['generation'], error, job['payload']['room'], viewer['user_id'], job['payload']['day'], job['id']))

    def retry_failed(self, role):
        # Retry means the original snapshot/configuration, never a silent model swap.
        markdown = self.settings()['analysis_mode'] == 'markdown_direct'
        with self.connect(True) as db:
            jobs = db.execute("SELECT * FROM jobs WHERE role=? AND status='failed' ORDER BY created_at", (role,)).fetchall()
            count = 0
            for job in jobs:
                payload = json.loads(job['payload'])
                if markdown and payload.get('pipeline_mode') != 'markdown_direct':
                    continue
                if payload.get('pipeline_mode') == 'markdown_direct':
                    v = payload['viewers'][0]
                    if db.execute('SELECT 1 FROM viewers WHERE room=? AND uid=? AND running_job IS NOT NULL',
                                  (payload['room'], v['user_id'])).fetchone():
                        continue
                    if db.execute("""SELECT 1 FROM jobs WHERE status='done'
                        AND json_extract(payload,'$.pipeline_mode')='markdown_direct'
                        AND json_extract(payload,'$.room')=? AND json_extract(payload,'$.viewers[0].user_id')=?
                        AND json_extract(payload,'$.viewers[0].input_revision')>=?""",
                                  (payload['room'], v['user_id'], v['input_revision'])).fetchone():
                        continue
                if role == 'analysis':
                    states = [db.execute('SELECT * FROM viewers WHERE room=? AND uid=? AND day=?', (payload['room'], v['user_id'], payload['day'])).fetchone() for v in payload['viewers']]
                    if any(s['running_job'] or s['published_gen'] >= v['generation'] for s, v in zip(states, payload['viewers'])):
                        continue
                    for v in payload['viewers']:
                        db.execute('UPDATE viewers SET running_job=?,error=NULL WHERE room=? AND uid=? AND day=?', (job['id'], payload['room'], v['user_id'], payload['day']))
                    if payload.get('pipeline_mode') in ('direct', 'markdown_direct'):
                        db.execute("UPDATE comments SET screening_status='processing' WHERE job_id=?", (job['id'],))
                else:
                    db.execute("UPDATE comments SET screening_status='processing' WHERE job_id=?", (job['id'],))
                db.execute("UPDATE jobs SET status='pending',attempts=0,retry_at=0 WHERE id=?", (job['id'],))
                count += 1
            return count

    def list_viewers(self, room, start, end, query='', view='all', active=0, stars=3, sort='time_desc', offset=0, limit=50):
        with self.connect() as db:
            conditions = ['c.room=?', 'c.day>=?', 'c.day<=?']
            args = [room, start, end]
            if query:
                pattern = '%' + query.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
                conditions.append("EXISTS(SELECT 1 FROM comments n WHERE n.room=c.room AND n.uid=c.uid AND n.nickname LIKE ? ESCAPE '\\')")
                args.append(pattern)
            if not query and active:
                conditions.append('c.uid IN (SELECT uid FROM comments WHERE room=? AND day>=? AND day<=? AND sent_at>=?)')
                args.extend((room, start, end, time.time() - active * 60))
            base = '''WITH candidates AS (
                SELECT c.uid,MAX(c.id) last_id,MAX(c.sent_at) last_at,COUNT(*) comment_count
                FROM comments c WHERE ''' + ' AND '.join(conditions) + ''' GROUP BY c.uid),
                ranked AS (SELECT v.*,r.body,r.created_at,r.config_id,
                    ROW_NUMBER() OVER(PARTITION BY v.uid ORDER BY v.day DESC) ranking
                    FROM viewers v LEFT JOIN results r ON r.id=v.result_id WHERE v.room=? AND v.day>=? AND v.day<=?)
                SELECT g.*,c.nickname,c.content,c.day,r.body,r.created_at,r.required_gen,r.published_gen,
                    r.running_job,r.error,r.config_id,
                    EXISTS(SELECT 1 FROM analysis_requests ar LEFT JOIN jobs j ON j.id=ar.job_id
                        WHERE ar.room=c.room AND ar.uid=g.uid AND (ar.job_id IS NULL OR j.status IN ('pending','running'))) manual_pending,
                    EXISTS(SELECT 1 FROM comments pending WHERE pending.room=c.room AND pending.uid=g.uid
                        AND pending.screening_status IN ('pending','processing')) awaiting_analysis,
                    json_extract(r.body,'$.intent_score') score,
                    json_extract(r.body,'$.needs_reply') needs_reply
                FROM candidates g JOIN comments c ON c.id=g.last_id
                LEFT JOIN ranked r ON r.uid=g.uid AND r.ranking=1'''
            args.extend((room, start, end))
            filters = []
            if not query and view == 'leads':
                filters.append('score>=?')
                args.append(stars)
            if not query and view == 'reply':
                filters.append('needs_reply=1')
            if filters:
                base += ' WHERE ' + ' AND '.join(filters)
            order = {'time_desc': 'last_at DESC', 'time_asc': 'last_at ASC',
                     'stars_desc': 'score DESC,last_at DESC', 'stars_asc': 'score IS NULL,score ASC,last_at DESC'}[sort]
            total = db.execute('SELECT COUNT(*) FROM (' + base + ')', args).fetchone()[0]
            rows = db.execute(base + ' ORDER BY ' + order + ',g.uid LIMIT ? OFFSET ?', (*args, limit, offset)).fetchall()
            previews = {}
            if rows:
                uids = [r['uid'] for r in rows]
                preview_rows = db.execute('''WITH recent AS (SELECT *,
                    ROW_NUMBER() OVER(PARTITION BY uid ORDER BY sent_at DESC,id DESC) ranking
                    FROM comments WHERE room=? AND day>=? AND day<=? AND uid IN ('''
                    + ','.join('?' for _ in uids) + ')) SELECT * FROM recent WHERE ranking<=6 ORDER BY sent_at,id',
                    (room, start, end, *uids)).fetchall()
                for row in preview_rows:
                    previews.setdefault(row['uid'], []).append({k: row[k] for k in
                        ('id', 'event_id', 'content', 'sent_at', 'platform_message_id')})
            return {'total': total, 'items': [{**dict(r), 'recent_comments': previews.get(r['uid'], []),
                'analysis': json.loads(r['body']) if r['body'] else None, 'body': None} for r in rows]}

    def history(self, room, uid, start, end, before=None, after=0, limit=50):
        with self.connect() as db:
            condition, args = 'room=? AND uid=? AND day>=? AND day<=?', [room, uid, start, end]
            if before:
                sent, seq = before
                condition += ' AND (sent_at<? OR (sent_at=? AND id<?))'
                args.extend((sent, sent, seq))
            if after:
                condition += ' AND id>?'
                args.append(after)
            ordering = 'id ASC' if after else 'sent_at DESC,id DESC'
            rows = db.execute('SELECT * FROM comments WHERE ' + condition + ' ORDER BY ' + ordering + ' LIMIT ?', (*args, limit + 1)).fetchall()
            more = len(rows) > limit
            rows = rows[:limit]
            items = [self.comment(r) for r in sorted(rows, key=lambda r: (r['sent_at'], r['id']))]
            latest = db.execute('SELECT COALESCE(MAX(id),0) FROM comments WHERE room=? AND uid=? AND day>=? AND day<=?', (room, uid, start, end)).fetchone()[0]
            cursor = [items[0]['sent_at'], items[0]['id']] if items and more and not after else None
            return {'items': items, 'before': cursor, 'has_more': more,
                    'revision': max((r['id'] for r in rows), default=after) if after else latest}

    def viewer_analysis(self, room, uid, day):
        with self.connect() as db:
            state = (db.execute('SELECT * FROM viewers WHERE room=? AND uid=? AND day=?', (room, uid, day)).fetchone() if day else
                     db.execute('SELECT * FROM viewers WHERE room=? AND uid=? ORDER BY day DESC LIMIT 1', (room, uid)).fetchone())
            if not state:
                return {'state': 'no_data', 'result': None}
            result = db.execute('SELECT * FROM results WHERE id=?', (state['result_id'],)).fetchone()
            if not day and not result:
                result = db.execute('SELECT * FROM results WHERE room=? AND uid=? ORDER BY day DESC,generation DESC LIMIT 1', (room, uid)).fetchone()
            scope = ' AND day=?' if day else ''
            args = (room, uid, day) if day else (room, uid)
            counts = dict(db.execute('SELECT screening_status,COUNT(*) FROM comments WHERE room=? AND uid=?' + scope + ' GROUP BY screening_status', args))
            manual = db.execute('''SELECT r.job_id,j.status FROM analysis_requests r LEFT JOIN jobs j ON j.id=r.job_id
                WHERE r.room=? AND r.uid=? AND (r.job_id IS NULL OR j.status IN ('pending','running'))
                ORDER BY r.created_at DESC LIMIT 1''', (room, uid)).fetchone()
            in_flight = db.execute('SELECT 1 FROM viewers WHERE room=? AND uid=? AND running_job IS NOT NULL', (room, uid)).fetchone() if not day else state['running_job']
            status = ('updating' if in_flight or state['required_gen'] > state['settled_gen'] else
                      'queued' if manual else
                      'failed' if state['error'] or counts.get('failed') else
                      'screening' if counts.get('pending') or counts.get('processing') else
                      'ready' if result else 'no_analysis_needed')
            return {'state': status, 'day': state['day'], 'manual_pending': bool(manual),
                    'manual_state': (manual['status'] or 'queued') if manual else None,
                    'pending_comments': counts.get('pending', 0) + counts.get('processing', 0),
                    'latest_revision': state['latest_revision'], 'required_generation': state['required_gen'],
                    'published_generation': state['published_gen'], 'error': state['error'],
                    'result': ({**dict(result), 'body': json.loads(result['body']), 'coverage': json.loads(result['coverage'])} if result else None)}

    def status(self):
        with self.connect() as db:
            return {'comments': db.execute('SELECT COUNT(*) FROM comments').fetchone()[0],
                    'screening': dict(db.execute('SELECT screening_status,COUNT(*) FROM comments GROUP BY screening_status')),
                    'jobs': [dict(r) for r in db.execute('SELECT role,status,COUNT(*) count FROM jobs GROUP BY role,status')],
                    'errors': [dict(r) for r in db.execute("SELECT role,error,created_at FROM jobs WHERE error IS NOT NULL AND status!='superseded' ORDER BY created_at DESC LIMIT 5")],
                    'rejected': db.execute('SELECT COUNT(*) FROM source_seen WHERE error IS NOT NULL').fetchone()[0],
                    'days': [r[0] for r in db.execute('SELECT DISTINCT day FROM comments ORDER BY day DESC LIMIT 60')]}

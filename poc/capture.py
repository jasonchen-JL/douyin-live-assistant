"""Thin Playwright CLI -> SQLite connector. No third-party Python packages."""

import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
import uuid

BASE = Path(__file__).resolve().parent
RUNTIME = BASE / 'runtime'


def now():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds')


class SessionLock:
    """OS lock is released after a crash; the lock file itself is not a PID claim."""
    def __init__(self, directory):
        self.path = Path(directory) / 'listener.lock'
        self.file = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open('a+b')
        if self.path.stat().st_size == 0:
            self.file.write(b'0')
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.file.close()
            raise RuntimeError('A collector already owns this browser session') from exc
        return self

    def __exit__(self, *_):
        self.file.close()


class BrowserCLI:
    timeout_seconds = 50
    interruption_check = None

    def __init__(self, session):
        if not re.fullmatch(r'[a-zA-Z0-9-]+', session):
            raise ValueError('Session must contain only letters, numbers and hyphens')
        self.session = session
        executable = shutil.which('playwright-cli.cmd') or shutil.which('playwright-cli')
        if not executable:
            raise RuntimeError('Install @playwright/cli first')
        entry = Path(executable).parent / 'node_modules/@playwright/cli/playwright-cli.js'
        # Call the npm entry via node to avoid cmd.exe quoting of paths and code.
        self.command = [shutil.which('node'), str(entry)] if entry.exists() else [executable]
        self.directory = RUNTIME / session
        self.directory.mkdir(parents=True, exist_ok=True)

    def call(self, *arguments, structured=False):
        command = [*self.command, '-s=' + self.session, '--raw', *arguments]
        if self.interruption_check is None:
            proc = subprocess.run(command, cwd=BASE, capture_output=True, text=True,
                                  encoding='utf-8', errors='replace', timeout=self.timeout_seconds)
        else:
            deadline = time.monotonic() + self.timeout_seconds
            with subprocess.Popen(command, cwd=BASE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True, encoding='utf-8', errors='replace') as child:
                try:
                    while True:
                        self.interruption_check()
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise subprocess.TimeoutExpired(command, self.timeout_seconds)
                        try:
                            stdout, stderr = child.communicate(timeout=min(0.2, remaining))
                            proc = subprocess.CompletedProcess(command, child.returncode, stdout, stderr)
                            break
                        except subprocess.TimeoutExpired:
                            pass
                except BaseException:
                    child.kill()
                    child.communicate()
                    raise
        output = proc.stdout.strip()
        if proc.returncode:
            raise RuntimeError('Playwright CLI failed: ' + (proc.stderr or output)[-800:])
        if not structured:
            if output.startswith(('### Error', 'Error:', 'TimeoutError:', 'SyntaxError:')):
                raise RuntimeError(output[:800])
            return output
        try:
            return json.loads(output)
        except (ValueError, TypeError) as exc:
            raise RuntimeError('CLI did not return JSON: ' + output[:400]) from exc

    def run(self, code):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.js', prefix='command-',
                                         dir=self.directory, encoding='utf-8', delete=False) as file:
            file.write(code)
            script = Path(file.name)
        try:
            return self.call('run-code', '--filename=' + str(script), structured=True)
        finally:
            script.unlink(missing_ok=True)

    def open(self, url, headed=False):
        args = ['open', url, '--config=' + str(BASE / 'cli.config.json'),
                '--profile=' + str(self.directory / 'profile')]
        if headed:
            args.append('--headed')
        self.call(*args)

    def install(self, config):
        adapter = config.get('adapter', 'dom')
        if adapter not in ('dom', 'douyin-react'):
            raise ValueError('Unknown observer adapter: ' + str(adapter))
        filename = 'observer-douyin.js' if adapter == 'douyin-react' else 'observer.js'
        observer = (BASE / filename).read_text(encoding='utf-8')
        health = (BASE / 'room-health.js').read_text(encoding='utf-8') if adapter == 'douyin-react' else '() => null'
        return self.run('async page => await page.evaluate(config => { window.__liveRoomHealth = (' + health +
                        '); return (' + observer + ')(config); }, ' + json.dumps(config) + ')')

    def navigate(self, url):
        timeout = max(1, int((self.timeout_seconds - 0.5) * 1000))
        # The lifecycle verifies the live room and observer separately after navigation.
        return self.run('async page => { await page.goto(' + json.dumps(url) +
                        ', {waitUntil: "commit", timeout: ' + str(timeout) +
                        '}); return true; }')

    def poll(self, ack=None, settings=None):
        return self.run('async page => await page.evaluate(args => '
                        'window.__liveCommentPoc?.poll(args.ack, args.settings) || null, ' +
                        json.dumps({'ack': ack, 'settings': settings}) + ')')

    def pause(self):
        return self.run('async page => await page.evaluate(() => window.__liveCommentPoc?.pause() || null)')

    def stop(self):
        return self.run('async page => await page.evaluate(() => { window.__liveCommentPoc?.stop(); return true; })')


class Store:
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=5)
        self._summaries = {}
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY, room_url TEXT NOT NULL,
                started_at TEXT NOT NULL, ended_at TEXT, status TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS comments (
                event_id TEXT PRIMARY KEY, run_id TEXT NOT NULL,
                document_id TEXT NOT NULL, seq INTEGER NOT NULL,
                room_url TEXT NOT NULL, nickname TEXT NOT NULL, content TEXT NOT NULL,
                user_id TEXT, user_id_kind TEXT, platform_message_id TEXT,
                source_time_raw TEXT, captured_at TEXT NOT NULL, phase TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS comments_platform_id
                ON comments(room_url, platform_message_id) WHERE platform_message_id IS NOT NULL;
            CREATE INDEX IF NOT EXISTS comments_user
                ON comments(room_url, user_id, captured_at) WHERE user_id IS NOT NULL;
            CREATE INDEX IF NOT EXISTS comments_run ON comments(run_id);
            CREATE TABLE IF NOT EXISTS samples (
                sample_id INTEGER PRIMARY KEY, run_id TEXT NOT NULL,
                sampled_at TEXT NOT NULL, metadata TEXT NOT NULL
            );
        ''')
        self.extra_fields = ('source_time_kind', 'message_type', 'common_message_id',
                             'data_source', 'platform_room_id')
        columns = {row[1] for row in self.db.execute('PRAGMA table_info(comments)')}
        for field in self.extra_fields:
            if field not in columns:
                self.db.execute('ALTER TABLE comments ADD COLUMN ' + field + ' TEXT')
        self.db.commit()

    def start(self, url):
        run_id = str(uuid.uuid4())
        with self.db:
            self.db.execute('INSERT INTO runs VALUES (?, ?, ?, NULL, ?)',
                            (run_id, url, now(), 'running'))
        self._summaries[run_id] = dict.fromkeys(('comments', 'with_user_id', 'with_source_time', 'live_comments'), 0)
        return run_id

    def save(self, run_id, batch):
        fields = ('event_id', 'document_id', 'seq', 'room_url', 'nickname', 'content',
                  'user_id', 'user_id_kind', 'platform_message_id', 'source_time_raw',
                  'captured_at', 'phase', *self.extra_fields)
        before = self.db.total_changes
        added = dict.fromkeys(('comments', 'with_user_id', 'with_source_time', 'live_comments'), 0)
        with self.db:
            for event in batch.get('events', []):
                values = [event.get(name) for name in fields]
                cursor = self.db.execute(
                    'INSERT INTO comments (run_id, ' + ', '.join(fields) + ') '
                    'VALUES (' + ','.join('?' for _ in range(len(fields) + 1)) + ') ON CONFLICT DO NOTHING',
                    [run_id, *values])
                if cursor.rowcount:
                    added['comments'] += 1
                    added['with_user_id'] += event.get('user_id') is not None
                    added['with_source_time'] += event.get('source_time_raw') is not None
                    added['live_comments'] += event.get('phase') == 'live'
            inserted = self.db.total_changes - before
            metadata = {key: value for key, value in batch.items() if key != 'events'}
            self.db.execute('INSERT INTO samples (run_id, sampled_at, metadata) VALUES (?, ?, ?)',
                            (run_id, now(), json.dumps(metadata, ensure_ascii=False)))
        if run_id in self._summaries:
            for field, count in added.items():
                self._summaries[run_id][field] += count
        return inserted

    def summary(self, run_id):
        if run_id in self._summaries:
            return dict(self._summaries[run_id])
        row = self.db.execute('''SELECT COUNT(*),
            COALESCE(SUM(user_id IS NOT NULL), 0),
            COALESCE(SUM(source_time_raw IS NOT NULL), 0),
            COALESCE(SUM(phase = 'live'), 0) FROM comments WHERE run_id = ?''', (run_id,)).fetchone()
        return dict(zip(('comments', 'with_user_id', 'with_source_time', 'live_comments'), row))

    def finish(self, run_id, status):
        with self.db:
            self.db.execute('UPDATE runs SET ended_at = ?, status = ? WHERE run_id = ?',
                            (now(), status, run_id))


def capture(browser, config, store, url, seconds, interval, *, stop_requested=None,
            runtime_settings=None, report=None, max_retries=5):
    if not math.isfinite(seconds) or seconds < 0 or not math.isfinite(interval) or interval <= 0:
        raise ValueError('Duration must be >= 0 and interval must be > 0, both finite')
    stop_requested = stop_requested or (lambda: False)
    run_id = store.start(url)
    ack = None
    document_id = None
    status = 'failed'
    deadline = time.monotonic() + seconds if seconds else math.inf
    warnings = set()
    retries = 0
    last = {}
    poll_ms = 0
    installed = False

    def emit(state):
        result = {'run_id': run_id, 'status': state, **store.summary(run_id),
                  'updated_at': now(), 'warnings': sorted(warnings), 'poll_ms': round(poll_ms),
                  'batch_interval_seconds': interval,
                  **{field: last.get(field) for field in (
                      'root_present', 'matched_rows', 'queued', 'overflow', 'list_found',
                      'scan_errors', 'schema_rejected', 'scan_interval_ms', 'last_scan_at',
                      'online', 'visibility', 'page_title', 'platform_room_ids')}}
        print(json.dumps(result), flush=True)
        if report:
            report(result)
        return result

    def accept(batch):
        nonlocal ack, document_id, last
        if batch['url'] != url or any(event['room_url'] != url for event in batch.get('events', [])):
            raise RuntimeError('Page changed room; stopping this capture run')
        if document_id and batch['document_id'] != document_id:
            warnings.add('page_reload_gap')
            store.save(run_id, {'event': 'document_changed_gap'})
            ack = None
        document_id = batch['document_id']
        for field in ('overflow', 'scan_errors', 'schema_rejected'):
            if batch.get(field, 0):
                warnings.add(field)
        if batch.get('online') is False:
            warnings.add('browser_offline')
        last = batch
        store.save(run_id, batch)
        # Only acknowledge after the SQLite transaction commits.
        if batch.get('events'):
            ack = {'document_id': document_id, 'seq': batch['events'][-1]['seq']}

    def wait(seconds_to_wait):
        end = min(deadline, time.monotonic() + seconds_to_wait)
        while time.monotonic() < end and not stop_requested():
            time.sleep(min(0.1, max(0, end - time.monotonic())))

    try:
        first = browser.install(config)
        installed = True
        accept({**first, 'event': 'observer_installed'})
        emit('running')
        while time.monotonic() < deadline and not stop_requested():
            settings = None
            if runtime_settings:
                try:
                    settings = runtime_settings()
                    interval = settings['batch_interval_seconds']
                except (ValueError, OSError) as error:
                    warnings.add('invalid_runtime_config')
                    store.save(run_id, {'event': 'invalid_runtime_config', 'error_type': type(error).__name__})
            started = time.monotonic()
            try:
                batch = browser.poll(ack, {'scanIntervalMs': settings['scan_interval_ms']}) if settings else browser.poll(ack)
                poll_ms = (time.monotonic() - started) * 1000
                retries = 0
            except (RuntimeError, subprocess.TimeoutExpired) as error:
                retries += 1
                warnings.add('browser_transport_gap')
                store.save(run_id, {'event': 'browser_transport_gap', 'attempt': retries,
                                    'error_type': type(error).__name__})
                emit('reconnecting')
                if retries >= max_retries:
                    raise
                wait(min(2 ** (retries - 1), 10))
                continue
            if batch is None:
                warnings.add('page_reload_gap')
                first = browser.install(config)
                accept({**first, 'event': 'page_reload_gap'})
                continue
            accept(batch)
            state = 'running'
            if batch.get('root_present') is False or batch.get('list_found') is False:
                state = 'source_unavailable'
                warnings.add('source_unavailable')
            if batch.get('queued', 0) > len(batch.get('events', [])):
                state = 'backlog'
            emit(state)
            if state != 'backlog':
                wait(interval)
        status = 'stopped' if stop_requested() else 'completed'
    except KeyboardInterrupt:
        status = 'interrupted'
    finally:
        try:
            if installed:
                emit('draining')
                paused = browser.pause()
                if paused is None:
                    warnings.add('shutdown_page_gap')
                elif paused['url'] != url or paused['document_id'] != document_id:
                    warnings.add('shutdown_page_gap')
                else:
                    # Freeze the producer, then persist every remaining page, not just 1000 rows.
                    while True:
                        batch = browser.poll(ack)
                        if batch is None:
                            raise RuntimeError('Observer disappeared during final drain')
                        accept(batch)
                        if not batch.get('events'):
                            break
                    browser.stop()
        except Exception:
            status = 'failed'
            raise
        finally:
            if status in ('completed', 'stopped', 'interrupted') and warnings:
                status += '_with_gaps'
            if status == 'completed' and not store.summary(run_id)['comments']:
                status = 'no_comments'
            store.finish(run_id, status)
            result = emit(status)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', required=True)
    parser.add_argument('--selectors', type=Path, default=BASE / 'selectors.example.json')
    parser.add_argument('--db', type=Path, default=RUNTIME / 'comments.sqlite3')
    parser.add_argument('--session', default='douyin-assistant-capture')
    parser.add_argument('--seconds', type=float, default=0, help='0 runs until stopped')
    parser.add_argument('--interval', type=float, default=1)
    parser.add_argument('--use-open-session', action='store_true')
    parser.add_argument('--headed', action='store_true')
    parser.add_argument('--allow-unverified-selectors', action='store_true')
    args = parser.parse_args()
    if not re.fullmatch(r'https://live\.douyin\.com/\d+/?', args.url):
        parser.error('--url must be a numeric https://live.douyin.com/ room URL')
    if not math.isfinite(args.seconds) or args.seconds < 0 or not math.isfinite(args.interval) or args.interval <= 0:
        parser.error('--seconds must be >= 0; --interval must be > 0; both must be finite')
    config = json.loads(args.selectors.read_text(encoding='utf-8-sig'))
    if str(config.get('status', '')).startswith('UNVERIFIED') and not args.allow_unverified_selectors:
        parser.error('Calibrate selectors using a real comment, or explicitly use '
                     '--allow-unverified-selectors for diagnostics only')
    browser = BrowserCLI(args.session)
    with SessionLock(browser.directory):
        store = Store(args.db)
        try:
            if not args.use_open_session:
                browser.open(args.url, args.headed)
            result = capture(browser, config, store, args.url.rstrip('/'), args.seconds, args.interval)
            print(json.dumps({**result, 'db': str(args.db.resolve())}), flush=True)
        finally:
            store.db.close()
            if not args.use_open_session:
                browser.call('close')


if __name__ == '__main__':
    main()

"""Local controls for the persistent Playwright CLI collector."""

import argparse
import json
import math
import os
from pathlib import Path
import sys
import time
from urllib.parse import urlsplit
import uuid

from capture import BASE, RUNTIME, BrowserCLI, SessionLock, Store, now
from recovery import Halt, ManagedCapture

DEFAULT_CONFIG = BASE / 'listener.json'


def load_settings(path):
    value = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    required = {'room_url', 'duration_seconds', 'scan_interval_ms', 'batch_interval_seconds'}
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError('Config must contain exactly: ' + ', '.join(sorted(required)))
    parsed = urlsplit(value['room_url'])
    room_id = parsed.path.strip('/')
    if (parsed.scheme != 'https' or parsed.netloc != 'live.douyin.com' or
            not room_id.isascii() or not room_id.isdigit() or parsed.path not in ('/' + room_id, '/' + room_id + '/')):
        raise ValueError('room_url must be a numeric https://live.douyin.com/ room URL')
    value['room_url'] = 'https://live.douyin.com/' + room_id
    for key, minimum, maximum in (
        ('duration_seconds', 0, 604800), ('scan_interval_ms', 50, 2000),
        ('batch_interval_seconds', 0.2, 10),
    ):
        number = value[key]
        if (isinstance(number, bool) or not isinstance(number, (int, float)) or
                not math.isfinite(number) or not minimum <= number <= maximum):
            raise ValueError(f'{key} must be finite and between {minimum} and {maximum}')
    return value


def session_for(settings):
    return 'douyin-assistant-' + settings['room_url'].rsplit('/', 1)[-1]


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    for attempt in range(3):
        try:
            temporary.replace(path)
            return
        except PermissionError:
            if attempt == 2:
                raise
            time.sleep(0.05)


def is_running(directory):
    if not (Path(directory) / 'listener.lock').exists():
        return False
    try:
        with SessionLock(directory):
            return False
    except RuntimeError:
        return True


def read_status(directory):
    path = Path(directory) / 'status.json'
    value = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {'status': 'not_started'}
    value['process_running'] = is_running(directory)
    if not value['process_running'] and value.get('status') in (
        'opening', 'running', 'backlog', 'source_unavailable', 'reconnecting', 'draining',
        'waiting_live', 'recovering', 'stopping'
    ):
        value['status'] = 'stale_process_exited'
        value.update(source_state='manual_required', source_ready=False, source_reason='collector_process_exited',
                     manual_restart_required=True)
    if value['process_running']:
        try:
            stop = json.loads((Path(directory) / 'stop.json').read_text(encoding='utf-8'))
            if stop.get('controller_id') == value.get('controller_id'):
                value.update(status='stopping', source_state='stopping', source_ready=False)
        except (OSError, ValueError):
            pass
    if not value['process_running']:
        value['source_ready'] = False
    return value


def request_stop(directory):
    state = read_status(directory)
    if not state['process_running']:
        return {'status': 'not_running'}
    if not state.get('controller_id'):
        raise ValueError('Use Ctrl+C to stop a collector started without listen.py')
    write_json(Path(directory) / 'stop.json', {'controller_id': state['controller_id']})
    return {'status': 'stop_requested', 'controller_id': state['controller_id']}


def ensure_browser(browser, url, before_reload=None, operate=None):
    operate = operate or (lambda function, *args, **kwargs: function(*args, **kwargs))
    listing = operate(browser.call, 'list', '--json', structured=True)
    existing = next((item for item in listing['browsers']
                     if item['name'] == browser.session and item['status'] == 'open'), None)
    if existing:
        if Path(existing['userDataDir']).resolve() != (browser.directory / 'profile').resolve():
            raise ValueError('Session belongs to a different profile; refusing to take it over')
        if before_reload:
            before_reload()
        operate(browser.navigate, url)
    else:
        operate(browser.open, url, headed=True)
    actual = operate(browser.run, 'async page => await page.evaluate(() => location.origin + location.pathname)')
    if actual.rstrip('/') != url:
        raise Halt('manual_required', 'room_changed')


def start(config_path, seconds_override=None, controller_id=None):
    settings = load_settings(config_path)
    session = session_for(settings)
    directory = RUNTIME / session
    controller_id = uuid.UUID(controller_id).hex if controller_id else uuid.uuid4().hex
    db_path = RUNTIME / ('room-' + settings['room_url'].rsplit('/', 1)[-1] + '.sqlite3')
    duration = settings['duration_seconds'] if seconds_override is None else seconds_override
    if not math.isfinite(duration) or duration < 0:
        raise ValueError('seconds must be finite and >= 0')
    identity = {'controller_id': controller_id, 'pid': os.getpid(), 'session': session,
                'room_url': settings['room_url'], 'db': str(db_path), 'duration_seconds': duration}

    def report(state):
        write_json(directory / 'status.json', {**state, **identity})

    def stopped():
        try:
            value = json.loads((directory / 'stop.json').read_text(encoding='utf-8'))
            return value.get('controller_id') == controller_id
        except (OSError, ValueError):
            return False

    def reload_settings():
        latest = load_settings(config_path)
        if (latest['room_url'] != settings['room_url'] or
                latest['duration_seconds'] != settings['duration_seconds']):
            raise ValueError('Room or duration changed; stop the current run before changing these settings')
        return latest

    with SessionLock(directory):
        report({'status': 'opening', 'source_state': 'opening', 'source_ready': False, 'updated_at': now()})
        store = None
        try:
            browser = BrowserCLI(session)
            observer_config = json.loads((BASE / 'selectors.douyin.json').read_text(encoding='utf-8-sig'))
            observer_config['scanIntervalMs'] = settings['scan_interval_ms']
            store = Store(db_path)
            return ManagedCapture(browser, observer_config, store, settings['room_url'], duration,
                                  settings['batch_interval_seconds'], stop_requested=stopped,
                                  runtime_settings=reload_settings, report=report,
                                  prepare=lambda drain, op: ensure_browser(browser, settings['room_url'], drain, op)).run()
        except BaseException as error:
            previous = read_status(directory)
            report({**previous, 'status': 'failed', 'source_state': 'manual_required', 'source_ready': False,
                    'manual_restart_required': True, 'updated_at': now(), 'error_type': type(error).__name__})
            raise
        finally:
            if store:
                store.db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('start', 'stop', 'status'))
    parser.add_argument('--config', type=Path, default=DEFAULT_CONFIG)
    parser.add_argument('--session', help='Explicit target for status/stop if room config was changed')
    parser.add_argument('--seconds', type=float, help='Override duration for a bounded verification run')
    parser.add_argument('--controller-id', help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        if args.command == 'start':
            if args.session:
                raise ValueError('Start derives its session from the room URL')
            result = start(args.config.resolve(), args.seconds, args.controller_id)
        else:
            session = args.session or session_for(load_settings(args.config))
            if not session.startswith('douyin-assistant-') or any(
                character not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-' for character in session
            ):
                raise ValueError('Invalid collector session')
            directory = RUNTIME / session
            result = request_stop(directory) if args.command == 'stop' else read_status(directory)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, RuntimeError, OSError) as error:
        print(json.dumps({'status': 'failed', 'error': str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

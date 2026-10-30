import json
import os
import subprocess
import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .config import ROOT


# Reuse the collector's existing configuration, OS lock and stop contract.
sys.path.insert(0, str(ROOT / 'poc'))
from listen import load_settings, read_status, request_stop, session_for, write_json  # noqa: E402
from capture import SessionLock  # noqa: E402


class Collector:
    def __init__(self, config_path=None):
        self.config_path = Path(config_path or ROOT / 'poc/listener.json')
        self.children = []
        self.control_lock = threading.Lock()
        self.launch_id = None

    def settings(self):
        return load_settings(self.config_path)

    def directory(self):
        return ROOT / 'poc/runtime' / session_for(self.settings())

    def source(self):
        return ROOT / 'poc/runtime' / ('room-' + self.settings()['room_url'].rsplit('/', 1)[-1] + '.sqlite3')

    def status(self):
        value = read_status(self.directory())
        if not value['process_running'] and any(p.poll() is None for p in self.children) and value.get('controller_id') != self.launch_id:
            value.update(status='opening', source_state='opening', source_ready=False, process_running=True)
            try:
                pending = json.loads((self.directory() / 'stop.json').read_text(encoding='utf-8'))
                if pending.get('controller_id') == self.launch_id:
                    value.update(status='stopping', source_state='stopping')
            except (OSError, ValueError):
                pass
        if value.get('source_ready'):
            try:
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(value['updated_at'])).total_seconds()
            except (KeyError, ValueError):
                age = 999
            if age > 30 or age < -5:
                value.update(source_state='recovering', source_ready=False, source_reason='collector_heartbeat_stale')
        allowed = ('status', 'process_running', 'updated_at', 'comments', 'queued', 'warnings',
                   'online', 'scan_errors', 'schema_rejected', 'platform_room_ids', 'room_url',
                   'source_state', 'source_ready', 'source_reason', 'manual_restart_required',
                   'recovery_id', 'recovery_started_at', 'recovery_deadline_at', 'recovery_attempts',
                   'next_retry_at', 'current_room_id', 'last_comment_at',
                   'last_browser_operation', 'last_browser_error')
        return {k: value.get(k) for k in allowed}

    def save(self, value):
        old = self.settings()
        state = self.status()
        if state['process_running'] and any(value[k] != old[k] for k in ('room_url', 'duration_seconds')):
            raise ValueError('collector_stop_before_room_change')
        write_json(self.config_path, value)

    def start(self):
        with self.control_lock:
            self.children = [p for p in self.children if p.poll() is None]
            state = self.status()
            if state['process_running'] or self.children:
                return state
            directory = self.directory()
            directory.mkdir(parents=True, exist_ok=True)
            self.launch_id = uuid.uuid4().hex
            with (directory / 'web-launch.log').open('ab') as output:
                child = subprocess.Popen([sys.executable, '-X', 'utf8', str(ROOT / 'poc/listen.py'), 'start',
                                          '--config', str(self.config_path), '--controller-id', self.launch_id], cwd=ROOT / 'poc',
                                         stdout=output, stderr=output,
                                         creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            self.children.append(child)
            return {'status': 'opening', 'source_state': 'opening', 'source_ready': False, 'process_running': True}

    def stop(self):
        with self.control_lock:
            state = request_stop(self.directory())
            if state['status'] == 'not_running' and any(p.poll() is None for p in self.children) and self.launch_id:
                write_json(self.directory() / 'stop.json', {'controller_id': self.launch_id})
                return {'status': 'stopping', 'source_state': 'stopping', 'source_ready': False}
            return state

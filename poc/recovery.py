"""Persistent collector lifecycle. Browser transport and storage remain existing adapters."""
import json
import math
import sqlite3
import subprocess
import time
import uuid

from capture import now


class Halt(Exception):
    def __init__(self, state, reason=None):
        self.state, self.reason = state, reason


def classify(batch, url):
    if not batch:
        return 'recovering', 'observer_missing'
    if batch.get('url', '').rstrip('/') != url:
        return 'manual_required', 'room_changed'
    health = batch.get('room_health') or {}
    if health.get('manual_reason'):
        return 'manual_required', health['manual_reason']
    if health.get('online', batch.get('online')) is False:
        return 'recovering', 'browser_offline'
    signal = health.get('room_signal') or {}
    if signal.get('web_rid') and signal['web_rid'] != url.rsplit('/', 1)[-1]:
        return 'manual_required', 'room_changed'
    if health.get('room_signal_conflict'):
        return 'recovering', 'room_state_conflict'
    if signal.get('room_status') in (4, '4') or health.get('ended_visible'):
        return 'waiting_live', 'live_ended'
    if signal.get('room_status') not in (2, '2') or not signal.get('room_id'):
        return 'recovering', 'room_state_unknown'
    if not batch.get('root_present') or not batch.get('list_found'):
        return 'recovering', 'comment_source_unavailable'
    if batch.get('paused') or not batch.get('last_scan_at') or not batch.get('scans'):
        return 'recovering', 'observer_not_ready'
    return 'ready', None


class ManagedCapture:
    def __init__(self, browser, config, store, url, seconds, interval, *, prepare,
                 stop_requested=lambda: False, runtime_settings=None, report=None,
                 monotonic=time.monotonic, wall=time.time, sleep=time.sleep,
                 recovery_seconds=1800, retry_seconds=15):
        self.browser, self.config, self.store, self.url = browser, config, store, url
        self.interval, self.prepare = interval, prepare
        self.stop_requested, self.runtime_settings, self.report = stop_requested, runtime_settings, report
        self.clock, self.wall, self.sleep = monotonic, wall, sleep
        self.deadline = monotonic() + seconds if seconds else math.inf
        self.recovery_seconds, self.retry_seconds = recovery_seconds, retry_seconds
        self.run_id = store.start(url)
        self.ack = self.document_id = None
        self.last = {}
        self.warnings = set()
        self.state, self.reason = 'opening', None
        self.recovery = None
        self.recovery_deadline = math.inf
        self.next_retry = monotonic()
        self.candidate = None
        self.heartbeat = None
        self.prepared = False
        self.browser_owned = False
        self.preserve_queue = False
        self.last_comment_at = None
        self.last_browser_operation = None
        self.last_browser_error = None
        self.begin_recovery()

    def begin_recovery(self):
        if self.recovery is None:
            stamp = self.wall()
            self.recovery = {'recovery_id': uuid.uuid4().hex, 'recovery_started_at': stamp,
                             'recovery_deadline_at': stamp + self.recovery_seconds,
                             'recovery_attempts': 0, 'next_retry_at': stamp + self.retry_seconds}
            self.recovery_deadline = self.clock() + self.recovery_seconds
            self.next_retry = self.clock() + self.retry_seconds

    def check(self):
        if self.stop_requested():
            raise Halt('stopped', 'manual_stop')
        if self.clock() >= self.recovery_deadline:
            raise Halt('stopped_timeout', 'recovery_timeout')
        if self.clock() >= self.deadline:
            raise Halt('completed', 'duration_reached')

    def operate(self, function, *args, **kwargs):
        self.check()
        # CLI navigation includes its own ten-second load wait after page.goto returns.
        cap = 20 if self.recovery is not None or getattr(function, '__name__', '') in ('navigate', 'open') else 8
        timeout = max(0.01, min(cap, self.recovery_deadline - self.clock(), self.deadline - self.clock()))
        previous = getattr(self.browser, 'interruption_check', None)
        self.browser.interruption_check = self.check
        try:
            result = self.browser_call(function, timeout, *args, **kwargs)
        finally:
            self.browser.interruption_check = previous
        self.check()
        return result

    def browser_call(self, function, timeout, *args, **kwargs):
        previous_timeout = getattr(self.browser, 'timeout_seconds', 50)
        self.browser.timeout_seconds = timeout
        operation = getattr(function, '__name__', '')
        if operation not in ('call', 'run', 'open', 'navigate', 'install', 'poll', 'pause', 'stop'):
            operation = 'browser_command'
        started = self.clock()
        record = {'operation': operation, 'started_at': self.wall(), 'timeout_seconds': timeout,
                  'status': 'ok'}
        try:
            return function(*args, **kwargs)
        except (RuntimeError, subprocess.TimeoutExpired) as error:
            record.update(status='error', error_type=type(error).__name__,
                          reason='command_timeout' if isinstance(error, subprocess.TimeoutExpired) else 'command_failed')
            if isinstance(error, RuntimeError) and 'page.goto: Timeout' in str(error):
                record['reason'] = 'navigation_timeout'
            self.last_browser_error = record
            raise
        finally:
            record['elapsed_seconds'] = round(self.clock() - started, 3)
            self.last_browser_operation = record
            self.browser.timeout_seconds = previous_timeout

    def emit(self, state=None, reason=None):
        if state:
            self.state, self.reason = state, reason
        ready = self.state == 'ready'
        value = {'run_id': self.run_id, 'status': 'running' if ready else self.state,
                 'source_state': self.state, 'source_ready': ready, 'source_reason': self.reason,
                 'manual_restart_required': self.state in ('stopped_timeout', 'manual_required'),
                 **self.store.summary(self.run_id), 'updated_at': now(), 'warnings': sorted(self.warnings),
                 'last_comment_at': self.last_comment_at, 'batch_interval_seconds': self.interval,
                 'last_browser_operation': self.last_browser_operation, 'last_browser_error': self.last_browser_error,
                 **{key: self.last.get(key) for key in ('root_present', 'list_found', 'queued', 'online',
                     'scan_errors', 'schema_rejected', 'platform_room_ids', 'last_scan_at')},
                 'current_room_id': ((self.last.get('room_health') or {}).get('room_signal') or {}).get('room_id'),
                 **(self.recovery or {key: None for key in ('recovery_id', 'recovery_started_at',
                     'recovery_deadline_at', 'recovery_attempts', 'next_retry_at')})}
        print(json.dumps(value), flush=True)
        if self.report:
            self.report(value)
        return value

    def accept(self, batch):
        if batch['url'].rstrip('/') != self.url or any(
                event['room_url'].rstrip('/') != self.url for event in batch.get('events', [])):
            raise Halt('manual_required', 'room_changed')
        if self.document_id and self.document_id != batch['document_id']:
            self.warnings.add('page_reload_gap')
            self.ack = None
        self.document_id, self.last = batch['document_id'], batch
        for field in ('overflow', 'scan_errors', 'schema_rejected'):
            if batch.get(field):
                self.warnings.add(field)
        self.store.save(self.run_id, batch)
        if batch.get('events'):
            last = batch['events'][-1]
            self.ack = {'document_id': self.document_id, 'seq': last['seq']}
            self.last_comment_at = last['captured_at']

    def drain(self, operate=None):
        operate = operate or self.operate
        paused = operate(self.browser.pause)
        if paused is None:
            return
        if paused['url'].rstrip('/') != self.url:
            raise Halt('manual_required', 'room_changed')
        # Pause first; persist all pages before sending the next ACK or navigating.
        while True:
            batch = operate(self.browser.poll, self.ack)
            if batch is None:
                self.warnings.add('drain_page_gap')
                return
            self.accept(batch)
            if not batch.get('events'):
                break
        operate(self.browser.stop)

    def reload(self):
        self.check()
        if self.recovery:
            self.recovery['recovery_attempts'] += 1
        self.emit('opening' if not self.prepared else self.state, self.reason)
        def before_reload():
            self.browser_owned = True
            try:
                self.drain()
            except (RuntimeError, subprocess.TimeoutExpired):
                self.warnings.add('drain_transport_gap')
            self.check()
            self.warnings.add('page_reload_gap')
        try:
            self.prepare(before_reload, self.operate)
            self.browser_owned = True
            self.prepared = True
            self.candidate = None
            self.ack = None
            self.operate(self.browser.install, self.config)
        finally:
            # Skip missed ticks; never replay accumulated refreshes after a slow operation.
            self.next_retry = self.clock() + self.retry_seconds
            if self.recovery:
                self.recovery['next_retry_at'] = self.wall() + self.retry_seconds

    def observe(self, batch):
        state, reason = classify(batch, self.url)
        if state == 'manual_required':
            raise Halt(state, reason)
        if batch:
            self.accept(batch)
        if state == 'ready':
            health = batch['room_health']
            stamp = (batch['document_id'], health['room_signal']['room_id'])
            scan = batch['last_scan_at']
            heartbeat = (batch['document_id'], batch['scans'], scan)
            if self.heartbeat is None or self.heartbeat[0] != heartbeat:
                self.heartbeat = (heartbeat, self.clock())
            if self.clock() - self.heartbeat[1] >= 15:
                self.candidate = None
                self.begin_recovery()
                self.emit('recovering', 'observer_stalled')
                return
            if self.candidate and self.candidate[0] == stamp:
                if scan != self.candidate[1] and self.clock() - self.candidate[2] >= 1:
                    self.check()
                    self.recovery = None
                    self.recovery_deadline = math.inf
                    self.emit('ready')
                    return
                if self.clock() - self.candidate[2] > 15 and scan == self.candidate[1]:
                    state, reason = 'recovering', 'observer_stalled'
                else:
                    self.emit('opening', 'confirming_source')
                    return
            else:
                self.begin_recovery()
                self.candidate = (stamp, scan, self.clock())
                self.emit('opening', 'confirming_source')
                return
        self.candidate = None
        self.begin_recovery()
        self.emit(state, reason)

    def wait(self):
        end = min(self.clock() + self.interval, self.deadline, self.recovery_deadline)
        while self.clock() < end:
            self.check()
            self.sleep(min(0.1, max(0, end - self.clock())))

    def run(self):
        terminal = 'failed'
        terminal_reason = 'collector_internal_error'
        try:
            self.emit('opening')
            first = True
            while True:
                self.check()
                if self.runtime_settings:
                    try:
                        settings = self.runtime_settings()
                        self.interval = settings['batch_interval_seconds']
                        self.config['scanIntervalMs'] = settings['scan_interval_ms']
                    except (ValueError, OSError):
                        self.warnings.add('invalid_runtime_config')
                try:
                    if first:
                        first = False
                        self.reload()
                    if not self.prepared:
                        if self.clock() >= self.next_retry:
                            self.reload()
                        self.wait()
                        continue
                    batch = self.operate(self.browser.poll, self.ack,
                                         {'scanIntervalMs': self.config.get('scanIntervalMs', 250)})
                    if batch is None and self.prepared:
                        # An externally reloaded page needs a new observer, not another navigation.
                        self.operate(self.browser.install, self.config)
                        batch = self.operate(self.browser.poll, None)
                    self.observe(batch)
                    if self.recovery and self.clock() >= self.next_retry and self.reason != 'browser_offline':
                        self.reload()
                except (RuntimeError, subprocess.TimeoutExpired):
                    self.warnings.add('browser_transport_gap')
                    self.candidate = None
                    self.begin_recovery()
                    self.emit('recovering', 'browser_transport_failed')
                    if self.clock() >= self.next_retry:
                        try:
                            self.reload()
                        except (RuntimeError, subprocess.TimeoutExpired):
                            pass
                self.wait()
        except Halt as halt:
            terminal, terminal_reason = halt.state, halt.reason
        except sqlite3.Error:
            self.preserve_queue = True
            self.warnings.add('storage_write_failed')
            terminal, terminal_reason = 'manual_required', 'storage_write_failed'
        except ValueError:
            terminal, terminal_reason = 'manual_required', 'browser_profile_mismatch'
        except KeyboardInterrupt:
            terminal, terminal_reason = 'stopped', 'manual_stop'
        finally:
            self.emit('stopping', terminal_reason)
            cleanup_deadline = self.clock() + 10
            def cleanup(function, *args):
                remaining = cleanup_deadline - self.clock()
                if remaining <= 0:
                    raise RuntimeError('cleanup_budget_exhausted')
                return self.browser_call(function, min(3, remaining), *args)
            try:
                if not self.browser_owned:
                    pass
                elif self.preserve_queue:
                    cleanup(self.browser.pause)
                else:
                    self.drain(cleanup)
            except (Exception, KeyboardInterrupt):
                self.warnings.add('shutdown_queue_at_risk')
            try:
                self.store.finish(self.run_id, terminal)
            except sqlite3.Error:
                self.warnings.add('storage_write_failed')
            result = self.emit(terminal, terminal_reason)
        return result

import asyncio
import time

from .models import ModelClient, ModelError
from .credentials import CredentialError, read_key
from .request_log import RequestLog


class Workers:
    def __init__(self, store, source_for, client=None):
        self.store, self.source_for = store, source_for
        self.request_log = RequestLog(store.path.parent / 'request-logs')
        self.client = client or ModelClient(request_log=self.request_log)
        self.tasks = []
        self.stopping = False
        self.running_tasks = set()
        self.active_jobs = {}
        self.drafts = {}
        self.request_log.write('request_logging_started')
        self.health = {'import_error': None, 'last_import_at': None, 'active': {},
                       'request_log_enabled': True, 'request_log_errors': self.request_log.failures}

    def start(self):
        self.store.recover()
        self.tasks = [asyncio.create_task(self.import_loop())]
        self.tasks.extend(asyncio.create_task(self.model_loop(role)) for role in ('screening', 'analysis'))

    async def stop(self):
        self.stopping = True
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        pending = list(self.running_tasks)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    async def import_loop(self):
        while not self.stopping:
            try:
                count = await asyncio.to_thread(self.store.import_source, self.source_for())
                self.health.update(import_error=None, last_import_at=time.time())
            except Exception:
                self.health['import_error'] = 'source_read_failed'
                count = 0
            await asyncio.sleep(0.05 if count >= 1000 else 1)

    async def run_once(self, role):
        job = self.store.claim(role)
        if not job:
            return False
        try:
            route = self.store.pool.acquire(job['routes'])
        except Exception:
            # No HTTP attempt occurred; keep the claimed snapshot available for retry.
            self.store.defer(job)
            self.health[role + '_waiting'] = 'pool_read_failed'
            return False
        if not route:
            self.store.defer(job)
            self.health[role + '_waiting'] = 'pool_unavailable'
            return False
        self.health.pop(role + '_waiting', None)
        self.active_jobs[job['id']] = {'role': role, 'started_at': time.time(), 'viewers': len(job['payload']['viewers'])}
        self.health['active'] = dict(self.active_jobs)
        failure = None
        outcome = {'status': 'error'}
        context = {'job_id': job['id'], 'attempt': job['attempt'], 'attempt_number': job['attempts'],
                   'role': role, 'pool_entry_id': route['id'], 'account_name': route.get('label')}
        self.request_log.write('job_attempt_started', **context)
        try:
            config = self.store.model(role, job['config_id'])
            config['base_url'] = route['base_url']
            if route['credential_ref']:
                config['api_key'] = read_key(route['credential_ref'])
            config['_before_request'] = lambda: self.store.pool.before_request(route)
            config['_diagnostic_context'] = {'pool_entry_id': route['id'], 'account_name': route.get('label')}
            if job['payload'].get('pipeline_mode') == 'markdown_direct':
                viewer = job['payload']['viewers'][0]
                last_update = 0

                def on_content(text):
                    nonlocal last_update
                    now = time.monotonic()
                    if now - last_update < 0.2:
                        return
                    last_update = now
                    self.drafts[job['id']] = {'job_id': job['id'], 'attempt': job['attempt'],
                        'room': job['payload']['room'], 'day': job['payload']['day'],
                        'uid': viewer['user_id'], 'analysis_markdown': text,
                        'coverage_count': len(viewer['comments']), 'generation': viewer['generation']}

                config['_on_content'] = on_content
            self.store.set_job_route(job, route)
            results = await self.client.analyze(job, config)
            published = self.store.complete(job, results)
            outcome.update(status='completed' if published else 'superseded')
        except ModelError as error:
            failure = error
            outcome.update(error_code=error.code, retryable=error.retryable or (
                not route.get('legacy') and error.code in ('model_http_401', 'model_http_403')))
            self.store.fail(job, error.code, error.retryable or (
                not route.get('legacy') and error.code in ('model_http_401', 'model_http_403')))
        except CredentialError as error:
            outcome.update(error_code=str(error), retryable=False)
            self.store.fail(job, str(error), False)
        except asyncio.CancelledError:
            outcome.update(status='cancelled', error_code='model_cancelled')
            # Recovery will retry the immutable job with a fresh attempt token.
            raise
        except Exception as error:
            outcome.update(error_code='analysis_internal_error', retryable=True,
                           exception_type=type(error).__name__)
            self.store.fail(job, 'analysis_internal_error')
        finally:
            self.request_log.write('job_attempt_finished', **context, **outcome,
                                   retry_pending=bool(outcome.get('retryable') and job['attempts'] < 3),
                                   elapsed_seconds=round(time.time() - self.active_jobs[job['id']]['started_at'], 6))
            self.health['request_log_errors'] = self.request_log.failures
            self.store.pool.release(route, failure)
            self.active_jobs.pop(job['id'], None)
            self.drafts.pop(job['id'], None)
            self.health['active'] = dict(self.active_jobs)
        return True

    async def model_loop(self, role):
        last = None
        while not self.stopping:
            try:
                limit = self.store.settings()['max_concurrent_requests']
                if len(self.running_tasks) >= limit:
                    await asyncio.sleep(0.05)
                    continue
                if last is not None and last.done() and not last.result():
                    interval = self.store.settings()['screening_interval_seconds'] if role == 'screening' else 0.5
                    last = None
                    await asyncio.sleep(interval)
                    continue
                last = asyncio.create_task(self.run_once(role))
                self.running_tasks.add(last)
                last.add_done_callback(lambda task: self.task_finished(task, role))
            except Exception:
                last = None
                self.health[role + '_error'] = 'worker_internal_error'
                await asyncio.sleep(0.5)
            await asyncio.sleep(0.05)

    def task_finished(self, task, role):
        self.running_tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            self.health[role + '_error'] = 'worker_internal_error'

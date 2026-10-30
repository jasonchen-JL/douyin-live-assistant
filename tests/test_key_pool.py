import asyncio
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from assistant_app.api import create_app
from assistant_app.config import PoolEntrySettings
from assistant_app.models import ModelClient, ModelError, retry_after_seconds
from assistant_app.store import Store
from assistant_app.workers import Workers
from .support import DAY, ROOM, IsolatedCollector, event, model_response, precise, screen


def add_account(store, name='one', secret=None, **extra):
    return store.pool.save(None, PoolEntrySettings(label=name, base_url='https://test.invalid',
        api_key=secret or 'fixture-account-' + name, min_interval_seconds=0, **extra).model_dump())


def select(store, ids, mode='direct'):
    store.save_settings({**store.settings(), 'analysis_mode': mode})
    for role in ('screening', 'analysis'):
        value = store.model(role, public=True)
        for name in ('version', 'configured', 'key_configured'):
            value.pop(name)
        store.save_model(role, {**value, 'pool_ids': ids})


def test_pool_secrets_and_endpoint_normalization(store):
    entry = add_account(store)
    assert entry['base_url'] == 'https://test.invalid/v1'
    assert entry['key_configured']
    assert not {'api_key', 'credential_ref', 'rate_key'} & entry.keys()
    with store.connect() as db:
        text = '\n'.join(db.iterdump())
    assert 'fixture-account-one' not in text
    reopened = Store(store.path)
    assert reopened.pool.entries() == store.pool.entries()


def test_direct_one_viewer_no_screening_call_and_inflight_new_comments(store, tmp_path):
    accounts = [add_account(store, str(i))['id'] for i in range(2)]
    select(store, accounts)
    store.ingest([event(1)])
    assert store.claim('screening') is None
    first = store.claim('analysis')
    assert len(first['payload']['viewers']) == 1
    assert first['payload']['pipeline_mode'] == 'direct'
    store.ingest([event(2)])
    assert store.claim('analysis') is None
    store.complete(first, precise(first))
    assert store.status()['screening'] == {'done': 1, 'pending': 1}
    second = store.claim('analysis')
    assert second['payload']['viewers'][0]['target_event_ids'] == ['synthetic-2']
    assert len(second['payload']['viewers'][0]['comments']) == 2
    store.complete(second, precise(second))
    assert not store.complete(first, precise(first))
    assert store.viewer_analysis(ROOM, event(1)['user_id'], DAY)['result']['coverage'] == ['synthetic-1', 'synthetic-2']


def test_two_stage_each_request_one_viewer_and_history_isolated(store):
    select(store, [add_account(store)['id']], 'two_stage')
    store.ingest([event(1, uid='a'), event(2, uid='b'), event(3, uid='a')])
    jobs = [store.claim('screening'), store.claim('screening')]
    assert [len(j['payload']['viewers']) for j in jobs] == [1, 1]
    assert jobs[0]['payload']['viewers'][0]['target_event_ids'] == ['synthetic-1', 'synthetic-3']
    for job in jobs:
        store.complete(job, screen(job))
    a, b = store.claim('analysis'), store.claim('analysis')
    assert len(a['payload']['viewers']) == len(b['payload']['viewers']) == 1
    assert a['payload']['viewers'][0]['user_id'] != b['payload']['viewers'][0]['user_id']


def test_duplicate_key_shares_capacity_across_roles_and_entries(store):
    a = add_account(store, 'a', 'same-fixture-secret')
    b = add_account(store, 'b', 'same-fixture-secret')
    routes = store.pool.routes([a['id'], b['id']])
    first = store.pool.acquire(routes)
    assert store.pool.acquire(routes) is None
    store.pool.release(first)
    assert store.pool.acquire(routes) is not None


def test_parallel_viewers_use_distinct_accounts_and_release_after_cancel(store, tmp_path):
    select(store, [add_account(store, str(i))['id'] for i in range(2)])
    store.ingest([event(i, uid=str(i)) for i in range(1, 4)])

    async def run():
        started, release = asyncio.Event(), asyncio.Event()
        calls = []

        class Client:
            async def analyze(self, job, config):
                await config['_before_request']()
                calls.append((job, config['api_key']))
                if len(calls) == 2:
                    started.set()
                await release.wait()
                return precise(job)

        worker = Workers(store, lambda: tmp_path / 'none', Client())
        tasks = [asyncio.create_task(worker.run_once('analysis')) for _ in range(3)]
        await asyncio.wait_for(started.wait(), 3)
        assert len({key for _, key in calls}) == 2
        assert len(calls) == 2
        assert sum(store.pool.active.values()) == 2
        release.set()
        results = await asyncio.gather(*tasks)
        assert results.count(True) == 2 and results.count(False) == 1
        assert sum(store.pool.active.values()) == 0
        with store.connect() as db:
            assert db.execute("SELECT attempts FROM jobs WHERE status='pending'").fetchone()[0] == 0

    asyncio.run(run())


def test_auth_failure_blocks_only_bad_account_and_retry_selects_other(store, tmp_path):
    bad, good = add_account(store, 'bad'), add_account(store, 'good')
    select(store, [bad['id'], good['id']])
    store.ingest([event(1)])
    seen = []

    class Client:
        async def analyze(self, job, config):
            seen.append(config['api_key'])
            if config['api_key'] == 'fixture-account-bad':
                raise ModelError('model_http_401', False)
            return precise(job)

    worker = Workers(store, lambda: tmp_path / 'none', Client())
    assert asyncio.run(worker.run_once('analysis'))
    with store.connect(True) as db:
        db.execute('UPDATE jobs SET retry_at=0')
    assert asyncio.run(worker.run_once('analysis'))
    assert seen == ['fixture-account-bad', 'fixture-account-good']
    assert next(e for e in store.pool.entries() if e['id'] == bad['id'])['blocked_reason'] == 'model_http_401'
    assert store.viewer_analysis(ROOM, event(1)['user_id'], DAY)['state'] == 'ready'


def test_429_cooldown_persists_and_manual_reset_does_not_bypass(store):
    entry = add_account(store)
    routes = store.pool.routes([entry['id']])
    route = store.pool.acquire(routes)
    store.pool.release(route, ModelError('model_http_429', retry_after=120))
    assert store.pool.acquire(routes) is None
    reopened = Store(store.path)
    reopened.pool.reset(entry['id'])
    assert reopened.pool.acquire(routes) is None
    assert reopened.pool.entries()[0]['cooldown_until'] > time.time() + 110


def test_job_routes_are_frozen_and_disabled_entry_cannot_be_used(store):
    account = add_account(store)
    select(store, [account['id']])
    store.ingest([event(1)])
    job = store.claim('analysis')
    store.pool.save(account['id'], PoolEntrySettings(label='changed', base_url='https://other.invalid/v1', enabled=False).model_dump())
    assert job['routes'][0]['base_url'] == 'https://test.invalid/v1'
    assert store.pool.acquire(job['routes']) is None
    store.defer(job, 0)
    retry = store.claim('analysis')
    assert retry['routes'] == job['routes']
    assert retry['attempts'] == 1


def test_new_attempt_can_use_repaired_key_without_mutating_old_lease(store):
    entry = add_account(store)
    routes = store.pool.routes([entry['id']])
    old = store.pool.acquire(routes)
    store.pool.release(old, ModelError('model_http_401', False))
    store.pool.save(entry['id'], PoolEntrySettings(label='fixed', base_url='https://other.invalid/v1', api_key='new-fixture-secret').model_dump())
    new = store.pool.acquire(routes)
    assert new['credential_ref'] != old['credential_ref']
    assert old['base_url'] == 'https://test.invalid/v1'
    assert new['base_url'] == 'https://other.invalid/v1'


def test_rotated_key_uses_current_usage_for_fair_selection(store):
    a, b = add_account(store, 'a'), add_account(store, 'b')
    routes = store.pool.routes([a['id'], b['id']])
    first = store.pool.acquire([routes[0]])
    store.pool.release(first)
    second = store.pool.acquire([routes[1]])
    store.pool.release(second)
    store.pool.save(a['id'], PoolEntrySettings(label='rotated', base_url='https://test.invalid',
                    api_key='rotated-fixture', min_interval_seconds=0).model_dump())
    rotated = store.pool.acquire(routes)
    assert rotated['id'] == a['id']
    store.pool.release(rotated)
    next_route = store.pool.acquire(routes)
    assert next_route['id'] == b['id']
    store.pool.release(next_route)


def test_pool_read_failure_defers_claim_without_consuming_attempt(store, tmp_path, monkeypatch):
    select(store, [add_account(store)['id']])
    store.ingest([event(1)])

    def unavailable(routes):
        raise RuntimeError('temporary pool read failure')

    monkeypatch.setattr(store.pool, 'acquire', unavailable)
    worker = Workers(store, lambda: tmp_path / 'none')
    assert not asyncio.run(worker.run_once('analysis'))
    assert worker.health['analysis_waiting'] == 'pool_read_failed'
    assert not worker.active_jobs and not store.pool.active
    with store.connect() as db:
        job = db.execute('SELECT status,attempts FROM jobs').fetchone()
        assert job['status'] == 'pending' and job['attempts'] == 0


def test_direct_retry_recovery_and_mode_switch_preserve_original_task(store):
    select(store, [add_account(store)['id']])
    store.ingest([event(1)])
    old = store.claim('analysis')
    store.ingest([event(2)])
    store.save_settings({**store.settings(), 'analysis_mode': 'two_stage'})
    assert store.claim('screening') is None
    store.recover()
    current = store.claim('analysis')
    assert current['payload'] == old['payload']
    assert not store.complete(old, precise(old))
    store.fail(current, 'model_invalid_json', False)
    assert store.status()['screening'] == {'failed': 1, 'pending': 1}
    assert store.retry_failed('analysis') == 1
    retried = store.claim('analysis')
    store.complete(retried, precise(retried))
    assert store.claim('screening')['payload']['viewers'][0]['target_event_ids'] == ['synthetic-2']


def test_api_pool_validation_and_secret_redaction(tmp_path):
    app = create_app(tmp_path, IsolatedCollector(tmp_path), background=False)
    client = TestClient(app)
    body = PoolEntrySettings(label='account', base_url='https://test.invalid', api_key='fixture-private').model_dump()
    assert client.post('/api/settings/key-pool', json=body).status_code == 403
    headers = {'X-Live-Assistant': '1'}
    response = client.post('/api/settings/key-pool', json=body, headers=headers)
    assert response.status_code == 200
    assert 'fixture-private' not in response.text
    entry_id = response.json()['id']
    body.update(api_key='', label='updated')
    assert client.put('/api/settings/key-pool/' + entry_id, json=body, headers=headers).status_code == 200
    response = client.get('/api/settings')
    assert response.json()['key_pool'][0]['label'] == 'updated'
    assert all(key not in response.text for key in ('fixture-private', 'credential_ref', 'rate_key'))
    body['max_concurrency'] = 'fixture-private'
    response = client.put('/api/settings/key-pool/' + entry_id, json=body, headers=headers)
    assert response.status_code == 422 and 'fixture-private' not in response.text


def test_model_retry_after_is_preserved(store):
    store.ingest([event(1)])
    job = store.claim('screening')
    transport = httpx.MockTransport(lambda request: httpx.Response(429, headers={'Retry-After': '135'}))
    with pytest.raises(ModelError) as caught:
        asyncio.run(ModelClient(transport).analyze(job, store.model('screening')))
    assert caught.value.retry_after == 135
    assert retry_after_seconds('invalid') == 60
    assert retry_after_seconds('NaN') == 60


def test_background_worker_obeys_global_limit(store, tmp_path):
    select(store, [add_account(store, str(i))['id'] for i in range(4)])
    store.save_settings({**store.settings(), 'max_concurrent_requests': 2})
    store.ingest([event(i, uid=str(i)) for i in range(1, 5)])

    async def run():
        active, highest = 0, 0

        class Client:
            async def analyze(self, job, config):
                nonlocal active, highest
                active += 1
                highest = max(highest, active)
                try:
                    await asyncio.sleep(0.3)
                    return precise(job)
                finally:
                    active -= 1

        worker = Workers(store, lambda: tmp_path / 'none', Client())
        worker.start()
        try:
            async def settled():
                while store.status()['screening'].get('done', 0) != 4:
                    await asyncio.sleep(0.1)
            await asyncio.wait_for(settled(), 10)
            assert highest == 2
        finally:
            await worker.stop()
        assert not worker.running_tasks and sum(store.pool.active.values()) == 0

    asyncio.run(run())


def test_cancelled_request_releases_account_and_recovery_changes_attempt(store, tmp_path):
    select(store, [add_account(store)['id']])
    store.ingest([event(1)])

    async def run():
        entered = asyncio.Event()
        frozen = []

        class Client:
            async def analyze(self, job, config):
                frozen.append(job)
                entered.set()
                await asyncio.Event().wait()

        worker = Workers(store, lambda: tmp_path / 'none', Client())
        task = asyncio.create_task(worker.run_once('analysis'))
        await asyncio.wait_for(entered.wait(), 3)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert sum(store.pool.active.values()) == 0
        store.recover()
        new = store.claim('analysis')
        assert new['attempt'] != frozen[0]['attempt']
        assert not store.complete(frozen[0], precise(frozen[0]))

    asyncio.run(run())

"""BR-16: batched previews, full history and durable manual Markdown commands."""
import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from assistant_app.api import create_app
from assistant_app.store import Store
from assistant_app.workers import Workers
from .support import DAY, MOMENT, ROOM, IsolatedCollector, configure, event
from .test_markdown import activate, result

UID = event(1)['user_id']


def request(store, uid=UID, request_id=None):
    return store.request_analysis(ROOM, uid, request_id or str(uuid4()))


def settled(store):
    activate(store)
    store.ingest([event(1)])
    job = store.claim('analysis')
    store.complete(job, result(job))
    return job


def test_six_previews_per_viewer_date_scope_same_name_and_order(store):
    store.ingest([event(i, uid='a' if i % 2 else 'b', at=MOMENT + i // 2) for i in range(1, 23)])
    store.ingest([event(30, uid='a', at=MOMENT - 86400)])
    found = store.list_viewers(ROOM, DAY, DAY)
    assert found['total'] == 2
    assert [v['uid'] for v in found['items']] == ['b', 'a']
    assert [len(v['recent_comments']) for v in found['items']] == [6, 6]
    assert [m['event_id'] for m in found['items'][0]['recent_comments']] == [f'synthetic-{i}' for i in range(12, 23, 2)]
    assert {m['event_id'] for v in found['items'] for m in v['recent_comments']} == {f'synthetic-{i}' for i in range(11, 23)}
    assert store.list_viewers(ROOM, DAY, DAY, offset=1, limit=1)['items'][0]['uid'] == 'a'
    # A delayed older comment is visible in history, without falsely topping activity order.
    store.ingest([event(31, uid='a', at=MOMENT - 1)])
    assert store.list_viewers(ROOM, DAY, DAY)['items'][0]['uid'] == 'b'


def test_manual_without_new_comments_bypasses_cache_and_keeps_old_result(store):
    old = settled(store)
    old_result = store.viewer_analysis(ROOM, UID, None)['result']
    command = request(store)
    assert request(store, request_id=command['request_id']) == command
    assert store.viewer_analysis(ROOM, UID, None)['state'] == 'queued'
    assert store.list_viewers(ROOM, DAY, DAY)['items'][0]['manual_pending']
    job = store.claim('analysis')
    assert job['id'] != old['id'] and job['payload']['trigger'] == 'manual'
    assert job['payload']['viewers'][0]['target_event_ids'] == []
    assert store.status()['screening'] == {'done': 1}
    assert store.viewer_analysis(ROOM, UID, None)['result'] == old_result
    assert store.claim('analysis') is None
    store.complete(job, result(job))
    state = store.viewer_analysis(ROOM, UID, None)
    assert state['result']['id'] != old_result['id']
    assert state['state'] == 'ready' and not state['manual_pending']
    assert request(store, request_id=command['request_id'])['state'] == 'done'
    assert store.claim('analysis') is None


def test_inflight_commands_coalesce_latest_snapshot_and_next_comments(store):
    activate(store)
    store.ingest([event(1)])
    first = store.claim('analysis')
    commands = [request(store) for _ in range(4)]
    store.ingest([event(2, at=MOMENT + 86400), event(3, uid='other')])
    other = store.claim('analysis')
    assert other['payload']['viewers'][0]['user_id'] == 'other'
    assert store.claim('analysis') is None
    store.complete(first, result(first))
    store.complete(other, result(other))
    following = store.claim('analysis')
    viewer = following['payload']['viewers'][0]
    assert len(viewer['comments']) == 2
    assert viewer['target_event_ids'] == ['synthetic-2']
    assert {request(store, request_id=c['request_id'])['job_id'] for c in commands} == {following['id']}
    store.ingest([event(4, at=MOMENT + 86401)])
    assert not store.complete(first, result(first))
    store.complete(following, result(following))
    last = store.claim('analysis')
    assert last['payload']['trigger'] == 'comments'
    assert last['payload']['viewers'][0]['target_event_ids'] == ['synthetic-4']
    store.complete(last, result(last))
    assert store.status()['screening'] == {'done': 4}
    assert store.claim('analysis') is None


def test_commands_are_atomic_and_concurrent_claims_do_not_overlap(store):
    settled(store)
    command_id = str(uuid4())
    with ThreadPoolExecutor(max_workers=4) as pool:
        commands = list(pool.map(lambda _: request(store, request_id=command_id), range(8)))
        jobs = list(pool.map(lambda _: store.claim('analysis'), range(4)))
    assert all(c['request_id'] == command_id for c in commands)
    assert sum(j is not None for j in jobs) == 1
    with store.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM analysis_requests').fetchone()[0] == 1


def test_manual_freezes_new_knowledge_background_config_only_when_claimed(store, tmp_path):
    knowledge = tmp_path / 'knowledge.md'
    knowledge.write_text('First reference', encoding='utf-8')
    store.knowledge_path = knowledge
    old = settled(store)
    request(store)
    knowledge.write_text('Updated reference', encoding='utf-8')
    store.save_settings({**store.settings(), 'topic': 'Updated topic'})
    job = store.claim('analysis')
    assert job['payload']['knowledge'] != old['payload']['knowledge']
    assert job['payload']['background']['topic'] == 'Updated topic'
    knowledge.write_text('Future reference', encoding='utf-8')
    store.fail(job, 'model_timeout')
    with store.connect(True) as db:
        db.execute('UPDATE jobs SET retry_at=0')
    retried = store.claim('analysis')
    assert retried['payload'] == job['payload']
    assert retried['config_id'] == job['config_id']


def test_recovery_keeps_commands_and_attempt_guard(store):
    settled(store)
    command = request(store)
    restarted = Store(store.path)
    restarted.recover()
    job = restarted.claim('analysis')
    restarted.recover()
    next_attempt = restarted.claim('analysis')
    assert next_attempt['id'] == job['id']
    assert next_attempt['attempt'] != job['attempt']
    assert not restarted.complete(job, result(job))
    assert restarted.complete(next_attempt, result(next_attempt))
    assert request(restarted, request_id=command['request_id'])['state'] == 'done'
    assert restarted.claim('analysis') is None


def test_manual_terminal_failure_retains_previous_and_can_rerun(store):
    settled(store)
    original = store.viewer_analysis(ROOM, UID, None)['result']
    command = request(store)
    failed = store.claim('analysis')
    store.fail(failed, 'model_timeout', False)
    state = store.viewer_analysis(ROOM, UID, None)
    assert state['state'] == 'failed' and state['error'] == 'model_timeout'
    assert state['result'] == original and not state['manual_pending']
    assert request(store, request_id=command['request_id'])['state'] == 'failed'
    request(store)
    new = store.claim('analysis')
    assert new['id'] != failed['id']
    store.complete(new, result(new))
    assert store.viewer_analysis(ROOM, UID, None)['state'] == 'ready'
    assert store.retry_failed('analysis') == 0


def test_all_scope_keeps_previous_day_success_while_new_comments_wait(store):
    settled(store)
    store.ingest([event(2, at=MOMENT + 86400)])
    state = store.viewer_analysis(ROOM, UID, None)
    assert state['result']['coverage'] == ['synthetic-1']
    assert state['pending_comments'] == 1 and state['day'] == '2026-09-08'


def test_api_command_guards_full_history_and_read_only_gets(tmp_path):
    app = create_app(tmp_path, IsolatedCollector(tmp_path), background=True)
    store = app.state.store
    configure(store)
    settled(store)
    store.ingest([event(2, at=MOMENT + 86400)])
    api = TestClient(app)
    route = f'/api/viewers/{UID}'
    assert len(api.get(route + f'/comments?start={DAY}').json()['items']) == 1
    assert len(api.get(route + f'/comments?scope=all&start={DAY}').json()['items']) == 2
    for _ in range(3):
        assert api.get(route + '/analysis?scope=all').status_code == 200
    with store.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == 1
        assert db.execute('SELECT COUNT(*) FROM analysis_requests').fetchone()[0] == 0
    body = {'room_url': ROOM, 'request_id': str(uuid4())}
    assert api.post(route + '/analysis', json=body).status_code == 403
    headers = {'X-Live-Assistant': '1'}
    assert api.post(route + '/analysis', json=body, headers={**headers, 'Origin': 'https://evil.invalid'}).status_code == 403
    assert api.post(route + '/analysis', json={**body, 'request_id': 'invalid'}, headers=headers).status_code == 422
    assert api.post(route + '/analysis', json={**body, 'room_url': ROOM + '1'}, headers=headers).status_code == 409
    assert api.post('/api/viewers/unknown/analysis', json=body, headers=headers).status_code == 404
    assert api.post(route + '/analysis', json=body, headers=headers).status_code == 202
    assert api.post('/api/viewers/unknown/analysis', json=body, headers=headers).json()['detail'] == 'analysis_request_conflict'
    store.save_settings({**store.settings(), 'analysis_enabled': False})
    assert api.post(route + '/analysis', json={**body, 'request_id': str(uuid4())}, headers=headers).json()['detail'] == 'analysis_paused'


def test_preview_service_cannot_accept_commands_without_workers(tmp_path):
    app = create_app(tmp_path, IsolatedCollector(tmp_path), background=False)
    configure(app.state.store)
    settled(app.state.store)
    api = TestClient(app)
    assert api.get('/api/status').json()['analysis_commands_enabled'] is False
    response = api.post(f'/api/viewers/{UID}/analysis', headers={'X-Live-Assistant': '1'},
                        json={'room_url': ROOM, 'request_id': str(uuid4())})
    assert response.status_code == 409 and response.json()['detail'] == 'analysis_worker_not_running'
    with app.state.store.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM analysis_requests').fetchone()[0] == 0


def test_manual_and_new_comments_run_through_worker_without_extra_get_calls(store, tmp_path):
    settled(store)
    calls = []
    class DelayedClient:
        async def analyze(self, job, config):
            calls.append(job)
            store.ingest([event(2)])
            assert store.viewer_analysis(ROOM, UID, None)['pending_comments'] == 1
            return result(job)
    workers = Workers(store, lambda: tmp_path / 'missing', DelayedClient())
    request(store)
    assert asyncio.run(workers.run_once('analysis'))
    assert len(calls) == 1 and calls[0]['payload']['viewers'][0]['target_event_ids'] == []
    following = store.claim('analysis')
    assert following['payload']['viewers'][0]['target_event_ids'] == ['synthetic-2']
    assert len(following['payload']['viewers'][0]['comments']) == 2

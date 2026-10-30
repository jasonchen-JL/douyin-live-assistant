import asyncio
import json
import sqlite3
from datetime import datetime

import pytest

from assistant_app.collector import SessionLock
from assistant_app.store import Store
from assistant_app.workers import Workers
from .support import DAY, MOMENT, ROOM, FixtureClient, event, precise, screen


def finish_screening(store, decision='analyze'):
    job = store.claim('screening')
    assert job
    assert store.complete(job, screen(job, decision))
    return job


def test_br02_dedup_and_identity(store):
    first = event(1)
    assert store.ingest([first, first, event(2)]) == 2
    assert store.ingest([{**first, 'event_id': 'another-observation'}]) == 0
    store.ingest([event(3, uid='other', nickname=first['nickname']), event(4, nickname='新昵称')])
    rows = store.list_viewers(ROOM, DAY, DAY)['items']
    assert len(rows) == 2
    assert {r['uid'] for r in rows} == {first['user_id'], 'other'}
    assert store.list_viewers(ROOM, DAY, DAY, query='测试观众')['total'] == 2
    assert store.list_viewers(ROOM, DAY, DAY, query='新昵称')['total'] == 1


def test_br10_platform_time_midnight_and_restarted_scene(store):
    midnight = datetime.fromisoformat('2026-09-08T00:00:00+08:00').timestamp()
    store.ingest([event(1, at=midnight - 1), event(2, at=midnight, scene='scene-2'),
                  event(3, at=midnight + 1, scene='scene-3')])
    assert store.list_viewers(ROOM, DAY, DAY)['items'][0]['comment_count'] == 1
    assert store.list_viewers(ROOM, '2026-09-08', '2026-09-08')['items'][0]['comment_count'] == 2
    first = finish_screening(store)
    assert first['payload']['day'] == DAY
    second = finish_screening(store)
    assert second['payload']['day'] == '2026-09-08'
    job = store.claim('analysis')
    store.complete(job, precise(job))
    job = store.claim('analysis')
    assert {c['platform_room_id'] for c in job['payload']['viewers'][0]['comments']} == {'scene-2', 'scene-3'}


def test_source_mirror_backlog_quarantine_and_no_mutation(store, tmp_path):
    from capture import Store as CollectorStore
    source = tmp_path / 'source.sqlite3'
    original = CollectorStore(source)
    run = original.start(ROOM)
    original.save(run, {'events': [event(i) for i in range(1, 106)] + [{**event(200), 'user_id': None}]})
    original.db.close()
    with sqlite3.connect(source) as db:
        before = '\n'.join(db.iterdump())
    assert store.import_source(source, limit=100) == 100
    assert store.import_source(source, limit=100) == 5
    assert store.import_source(source) == 0
    assert store.status()['comments'] == 105
    assert store.status()['rejected'] == 1
    with sqlite3.connect(source) as db:
        assert '\n'.join(db.iterdump()) == before
    with pytest.raises(ValueError):
        store.import_source(store.path)


def test_br03_history20_is_per_viewer_and_independent_of_new_limit(store):
    store.save_settings({**store.settings(), 'screening_batch_limit': 100})
    store.ingest([event(i, uid='a' if i % 2 else 'b') for i in range(1, 61)])
    finish_screening(store, 'skip')
    finish_screening(store, 'skip')
    store.save_settings({**store.settings(), 'screening_batch_limit': 3})
    store.ingest([event(61, uid='a'), event(62, uid='b'), event(63, uid='a'), event(64, uid='b')])
    job = store.claim('screening')
    viewers = job['payload']['viewers']
    assert sum(len(v['new_comments']) for v in viewers) == 2
    assert [len(v['history_comments']) for v in viewers] == [20]
    assert viewers[0]['history_comments'][0]['event_id'] == 'synthetic-21'
    assert store.status()['screening']['pending'] == 2
    other = store.claim('screening')['payload']['viewers']
    assert len(other) == 1 and len(other[0]['history_comments']) == 20


def test_br07_inflight_screening_never_marks_new_insert_done(store):
    store.ingest([event(1)])
    job = store.claim('screening')
    store.ingest([event(2)])
    store.complete(job, screen(job))
    assert store.status()['screening'] == {'done': 1, 'pending': 1}
    assert job['payload']['viewers'][0]['target_event_ids'] == ['synthetic-1']


def test_br04_needs_reply_never_silently_skipped(store):
    store.ingest([event(1)])
    job = store.claim('screening')
    results = screen(job, 'skip')
    results[0]['needs_reply'] = True
    store.complete(job, results)
    assert store.claim('analysis') is not None


def test_precision_reuses_identical_input_not_generation_or_date(store):
    store.ingest([event(1)])
    finish_screening(store)
    store.ingest([event(2)])
    job = store.claim('analysis')
    store.complete(job, precise(job))
    original = store.viewer_analysis(ROOM, event(1)['user_id'], DAY)['result']
    finish_screening(store)
    assert store.claim('analysis') is None
    state = store.viewer_analysis(ROOM, event(1)['user_id'], DAY)
    assert state['published_generation'] == 2
    assert state['result'] == original
    assert state['state'] == 'ready'


def test_auth_failure_pauses_new_jobs_for_same_configuration(store):
    store.ingest([event(1)])
    failed = store.claim('screening')
    store.fail(failed, 'model_http_401', False)
    store.ingest([event(2)])
    assert store.claim('screening') is None
    assert store.status()['screening'] == {'failed': 1, 'pending': 1}
    config = {k: v for k, v in store.model('screening').items() if k not in ('version', 'configured')}
    store.save_model('screening', {**config, 'model': 'corrected-model'})
    assert store.claim('screening') is not None


def test_br07_precision_followup_merges_and_skip_preserves_result(store):
    uid = event(1)['user_id']
    store.ingest([event(1)])
    finish_screening(store)
    old = store.claim('analysis')
    assert len(old['payload']['viewers'][0]['comments']) == 1
    for seq in (2, 3):
        store.ingest([event(seq)])
        finish_screening(store)
    assert store.claim('analysis') is None
    store.complete(old, precise(old))
    assert store.viewer_analysis(ROOM, uid, DAY)['state'] == 'updating'
    followup = store.claim('analysis')
    assert followup['payload']['viewers'][0]['generation'] == 3
    assert len(followup['payload']['viewers'][0]['comments']) == 3
    store.complete(followup, precise(followup, 5))
    result = store.viewer_analysis(ROOM, uid, DAY)['result']
    store.ingest([event(4, content='谢谢')])
    finish_screening(store, 'skip')
    assert store.claim('analysis') is None
    assert store.viewer_analysis(ROOM, uid, DAY)['result'] == result
    assert not store.complete(old, precise(old, 1))
    assert store.viewer_analysis(ROOM, uid, DAY)['result']['body']['intent_score'] == 5


@pytest.mark.parametrize('role', ['screening', 'analysis'])
def test_recovery_new_attempt_rejects_late_response(store, role):
    store.ingest([event(1)])
    if role == 'analysis':
        finish_screening(store)
    old = store.claim(role)
    store.recover()
    new = store.claim(role)
    results = screen(old) if role == 'screening' else precise(old)
    assert new['id'] == old['id'] and new['attempt'] != old['attempt']
    assert new['payload'] == old['payload']
    assert not store.complete(old, results)
    assert store.complete(new, results)


@pytest.mark.parametrize('role', ['screening', 'analysis'])
def test_atomic_commit_and_failed_retry_frozen_config(store, role):
    store.ingest([event(1, uid='a'), event(2, uid='b')])
    if role == 'analysis':
        finish_screening(store)
        finish_screening(store)
    job = store.claim(role)
    with pytest.raises(ValueError):
        store.complete(job, [])
    with store.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM results').fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM comments WHERE screening_status='done'").fetchone()[0] == (2 if role == 'analysis' else 0)
    config = store.model(role)
    value = {k: v for k, v in config.items() if k not in ('version', 'configured')}
    store.save_model(role, {**value, 'model': 'changed-model'})
    store.fail(job, 'model_http_401', False)
    assert store.retry_failed(role) == 1
    retried = store.claim(role)
    assert retried['config_id'] == job['config_id']
    assert store.model(role, retried['config_id'])['model'] == 'synthetic-fixture'
    assert store.complete(retried, screen(retried) if role == 'screening' else precise(retried))


def test_br08_search_bypasses_lead_filters_and_escapes_wildcards(store):
    store.ingest([event(1, nickname='a_100%'), event(2, uid='u2', nickname='aX100z')])
    finish_screening(store)
    job = store.claim('analysis')
    store.complete(job, precise(job, 1))
    assert store.list_viewers(ROOM, DAY, DAY, view='leads', active=1)['total'] == 0
    found = store.list_viewers(ROOM, DAY, DAY, query='a_100%', view='leads', active=1)
    assert found['total'] == 1 and found['items'][0]['score'] == 1


def test_history_keyset_and_late_event_delta(store):
    store.ingest([event(i, at=MOMENT) for i in range(1, 121)])
    uid = event(1)['user_id']
    first = store.history(ROOM, uid, DAY, DAY)
    second = store.history(ROOM, uid, DAY, DAY, before=first['before'])
    third = store.history(ROOM, uid, DAY, DAY, before=second['before'])
    all_ids = [c['event_id'] for batch in (first, second, third) for c in batch['items']]
    assert len(set(all_ids)) == len(all_ids) == 120
    store.ingest([event(200, at=MOMENT - 60)])
    delta = store.history(ROOM, uid, DAY, DAY, after=first['revision'])
    assert [c['event_id'] for c in delta['items']] == ['synthetic-200']
    assert store.history(ROOM, uid, DAY, DAY, after=delta['revision'])['items'] == []


def test_worker_chain_persists_results_without_live_browser(store, tmp_path):
    store.ingest([event(1), event(2, uid='second')])
    workers = Workers(store, lambda: tmp_path / 'missing-source', FixtureClient())
    assert asyncio.run(workers.run_once('screening'))
    assert asyncio.run(workers.run_once('analysis'))
    assert asyncio.run(workers.run_once('screening'))
    assert asyncio.run(workers.run_once('analysis'))
    assert not asyncio.run(workers.run_once('screening'))
    reopened = Store(store.path)
    assert reopened.list_viewers(ROOM, DAY, DAY, view='leads')['total'] == 2


def test_service_lock_is_exclusive(tmp_path):
    with SessionLock(tmp_path / 'service'):
        with pytest.raises(RuntimeError):
            with SessionLock(tmp_path / 'service'):
                pass

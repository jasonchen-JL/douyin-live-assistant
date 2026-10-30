"""BR-14: single-viewer Markdown, immutable coverage and legacy rollback boundary."""
import asyncio
import json
from datetime import datetime

import httpx
import pytest
from fastapi.testclient import TestClient

from assistant_app.api import create_app
from assistant_app.models import ModelClient, ModelError, normalize_markdown
from assistant_app.store import Store
from assistant_app.workers import Workers
from .support import DAY, MOMENT, ROOM, IsolatedCollector, configure, event, precise, screen
from .test_streaming import delta, frame, response

TEXT = '**主播直接说：** 每天有多少次订单查询？\n\n**观众背景与需求**：电商客服希望减少重复工作。'


def activate(store):
    store.save_settings({**store.settings(), 'analysis_mode': 'markdown_direct'})


def result(job, text=TEXT):
    return [{'user_id': job['payload']['viewers'][0]['user_id'], 'analysis_markdown': text,
             'format': 'markdown_v1', 'intent_score': None, 'needs_reply': None}]


def test_default_and_once_only_upgrade(tmp_path):
    path = tmp_path / 'assistant.sqlite3'
    store = Store(path)
    assert store.settings()['analysis_mode'] == 'markdown_direct'
    store.save_settings({**store.settings(), 'analysis_mode': 'two_stage', 'topic': 'preserve'})
    with store.connect(True) as db:
        db.execute('DELETE FROM app_migrations')
    upgraded = Store(path)
    assert upgraded.settings()['analysis_mode'] == 'markdown_direct'
    assert upgraded.settings()['topic'] == 'preserve'
    upgraded.save_settings({**upgraded.settings(), 'analysis_mode': 'direct'})
    assert Store(path).settings()['analysis_mode'] == 'direct'  # rollback is not forcibly undone


@pytest.mark.parametrize('json_mode', ['json_object', 'json_schema', 'prompt'])
def test_plain_protocol_backend_identity_and_no_optional_fields(store, json_mode):
    activate(store)
    store.ingest([event(1)])
    job = store.claim('analysis')
    calls = []

    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        assert 'response_format' not in body and body['stream'] is True
        assert 'JSON Schema' not in body['messages'][0]['content']
        assert body['messages'][2]['content'].startswith('**主播直接说：**')  # few-shot
        actual = json.loads(body['messages'][-1]['content'])
        assert actual['comments'][0]['new'] is True
        for forbidden in ('user_id', 'event_id', 'target_event_ids', job['payload']['viewers'][0]['user_id']):
            assert forbidden not in body['messages'][-1]['content']
        return response([frame(delta(reasoning='must-not-render')),
                         frame(delta(content=TEXT, finish='stop')), frame('[DONE]')])

    output = asyncio.run(ModelClient(httpx.MockTransport(handler)).analyze(job, {**store.model('analysis'), 'json_mode': json_mode}))
    assert output == result(job) and len(calls) == 1
    store.complete(job, output)
    viewer = store.list_viewers(ROOM, DAY, DAY)['items'][0]
    assert viewer['score'] is None and viewer['needs_reply'] is None
    assert viewer['analysis']['analysis_markdown'] == TEXT
    assert store.claim('screening') is None and store.claim('analysis') is None


def test_all_history_cross_midnight_and_inflight_comments(store):
    activate(store)
    midnight = datetime.fromisoformat('2026-09-08T00:00:00+08:00').timestamp()
    store.ingest([event(1, at=midnight - 1), event(2, at=midnight, scene='scene-2'),
                  event(3, uid='other', nickname='测试观众', at=midnight)])
    first = store.claim('analysis')
    assert first['payload']['day'] == '2026-09-08'
    assert [c['event_id'] for c in first['payload']['viewers'][0]['comments']] == ['synthetic-1', 'synthetic-2']
    store.ingest([event(4, at=midnight + 1)])
    second_viewer = store.claim('analysis')
    assert second_viewer['payload']['viewers'][0]['user_id'] == 'other'
    assert store.claim('analysis') is None
    store.complete(first, result(first))
    next_job = store.claim('analysis')
    assert next_job['payload']['viewers'][0]['target_event_ids'] == ['synthetic-4']
    assert len(next_job['payload']['viewers'][0]['comments']) == 3
    assert not store.complete(first, result(first, 'stale'))
    store.complete(next_job, result(next_job))
    store.complete(second_viewer, result(second_viewer))
    assert store.status()['screening'] == {'done': 4}
    assert store.claim('analysis') is None
    assert len(store.viewer_analysis(ROOM, event(1)['user_id'], '2026-09-08')['result']['coverage']) == 3


@pytest.mark.parametrize('stage', ['screening', 'analysis', 'waiting_analysis'])
@pytest.mark.parametrize('terminal', [False, True])
def test_legacy_pending_jobs_not_called_after_switch(store, stage, terminal):
    store.ingest([event(1)])
    old = store.claim('screening')
    if stage != 'screening':
        store.complete(old, screen(old))
        old = store.claim('analysis') if stage == 'analysis' else None
    if old:
        store.fail(old, 'model_timeout', not terminal)
    activate(store)
    assert store.claim('screening') is None
    new = store.claim('analysis')
    assert new['payload']['pipeline_mode'] == 'markdown_direct'
    if old:
        assert not store.complete(old, precise(old) if stage == 'analysis' else screen(old))
    store.complete(new, result(new))
    assert store.status()['screening'] == {'done': 1}
    assert store.claim('analysis') is None


def test_cross_day_retry_does_not_overlap_or_revert_newer_success(store):
    activate(store)
    store.ingest([event(1)])
    old = store.claim('analysis')
    store.fail(old, 'model_timeout', False)
    store.ingest([event(2, at=MOMENT + 86400)])
    new = store.claim('analysis')
    assert store.retry_failed('analysis') == 0
    store.complete(new, result(new))
    assert store.retry_failed('analysis') == 0


def test_live_mode_switch_does_not_start_second_request_for_inflight_viewer(store):
    store.save_settings({**store.settings(), 'analysis_mode': 'direct'})
    store.ingest([event(1)])
    old = store.claim('analysis')
    activate(store)
    store.ingest([event(2)])
    assert store.claim('analysis') is None
    store.complete(old, precise(old))
    new = store.claim('analysis')
    assert new['payload']['pipeline_mode'] == 'markdown_direct'
    assert new['payload']['viewers'][0]['target_event_ids'] == ['synthetic-2']
    store.complete(new, result(new))
    assert store.retry_failed('analysis') == 0


def test_legacy_success_is_kept_until_new_comment(store):
    store.ingest([event(1)])
    old = store.claim('screening')
    store.complete(old, screen(old))
    old = store.claim('analysis')
    store.complete(old, precise(old))
    activate(store)
    assert store.claim('analysis') is None
    assert store.viewer_analysis(ROOM, event(1)['user_id'], DAY)['result']['body']['intent_score'] == 4
    store.ingest([event(2)])
    new = store.claim('analysis')
    assert len(new['payload']['viewers'][0]['comments']) == 2
    store.fail(new, 'model_timeout', False)
    assert store.viewer_analysis(ROOM, event(1)['user_id'], DAY)['result']['body']['intent_score'] == 4
    assert store.retry_failed('analysis') == 1
    retry = store.claim('analysis')
    assert retry['payload'] == new['payload']
    store.complete(retry, result(retry))
    assert store.viewer_analysis(ROOM, event(1)['user_id'], DAY)['result']['body']['intent_score'] is None


@pytest.mark.parametrize('text,expected', [
    ('', 'model_empty_content'), ('{"summary":"missing"}', 'model_missing_opening'),
    ('分析如下\n\n' + TEXT, 'model_missing_opening'), ('**主播直接说：** ', 'model_missing_opening'),
])
def test_only_required_opening_validated(text, expected):
    with pytest.raises(ModelError, match=expected):
        normalize_markdown(text)


@pytest.mark.parametrize('text', [TEXT, '```markdown\n' + TEXT + '\n```', '## 主播直接说：每天有多少次订单查询？'])
def test_markdown_opening_tolerates_basic_variants(text):
    assert normalize_markdown(text).startswith('**主播直接说：** 每天有多少次订单查询？')


def test_history_budget_never_silently_truncates_or_calls_summary(store):
    activate(store)
    store.ingest([event(1, content='历史' * 60000)])
    job = store.claim('analysis')
    def forbidden(request):
        raise AssertionError('No HTTP for oversized input')
    with pytest.raises(ModelError, match='input_budget_exceeded'):
        asyncio.run(ModelClient(httpx.MockTransport(forbidden)).analyze(job, store.model('analysis')))
    assert store.status()['screening'] == {'processing': 1}


def test_new_comment_retries_previously_failed_messages_in_same_full_snapshot(store):
    activate(store)
    store.ingest([event(1)])
    failed = store.claim('analysis')
    store.fail(failed, 'model_timeout', False)
    assert store.claim('analysis') is None  # finite retry, no failed-only automatic loop
    store.ingest([event(2)])
    new = store.claim('analysis')
    assert new['payload']['viewers'][0]['target_event_ids'] == ['synthetic-1', 'synthetic-2']
    store.complete(new, result(new))
    assert store.status()['screening'] == {'done': 2}
    assert store.viewer_analysis(ROOM, event(1)['user_id'], DAY)['state'] == 'ready'
    assert store.retry_failed('analysis') == 0


@pytest.mark.parametrize('finish', ['stop', 'length'])
def test_preview_before_finish_and_cleanup_without_false_success(tmp_path, finish):
    app = create_app(tmp_path, IsolatedCollector(tmp_path), background=False)
    store, worker = app.state.store, app.state.workers
    configure(store)
    activate(store)
    store.ingest([event(1)])
    api = TestClient(app)

    async def scenario():
        emitted, release = asyncio.Event(), asyncio.Event()
        class Live(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield frame(delta(reasoning='hidden-reasoning'))
                yield frame(delta(content=TEXT))
                emitted.set()
                await release.wait()
                yield frame(delta(finish=finish))
                yield frame('[DONE]')
        worker.client = ModelClient(httpx.MockTransport(lambda r: httpx.Response(
            200, headers={'content-type': 'text/event-stream'}, stream=Live())))
        task = asyncio.create_task(worker.run_once('analysis'))
        try:
            await asyncio.wait_for(emitted.wait(), 10)
            value = api.get(f"/api/viewers/{event(1)['user_id']}/analysis?day={DAY}").json()
            assert value['result'] is None and value['preview']['analysis_markdown'] == TEXT
            assert store.status()['screening'] == {'processing': 1}
        finally:
            release.set()
            await task
        assert not worker.drafts
        assert store.status()['screening'] == ({'done': 1} if finish == 'stop' else {'processing': 1})
        value = api.get(f"/api/viewers/{event(1)['user_id']}/analysis?day={DAY}").json()
        assert 'preview' not in value
        assert bool(value['result']) == (finish == 'stop')
    asyncio.run(scenario())

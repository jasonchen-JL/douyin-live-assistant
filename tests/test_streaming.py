import asyncio
import copy
import json
import time

import httpx
import pytest

from assistant_app.config import ModelSettings
from assistant_app.models import ModelClient, ModelError, SingleAnalysis, SingleScreening, few_shot
from assistant_app.request_log import RequestLog
from assistant_app.workers import Workers
from .support import DAY, ROOM, event, model_response, precise, screen


def frame(value):
    return ('data: ' + (value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)) + '\n\n').encode()


def delta(content=None, reasoning=None, finish=None):
    value = {}
    if content is not None:
        value['content'] = content
    if reasoning is not None:
        value['reasoning_content'] = reasoning
    return {'choices': [{'index': 0, 'delta': value, 'finish_reason': finish}]}


class BytesStream(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks, self.closed = chunks, False

    async def __aiter__(self):
        for chunk in self.chunks:
            await asyncio.sleep(0)
            yield chunk

    async def aclose(self):
        self.closed = True


def response(chunks):
    return httpx.Response(200, headers={'content-type': 'text/event-stream; charset=utf-8'}, stream=BytesStream(chunks))


@pytest.mark.parametrize('role', ['screening', 'analysis'])
@pytest.mark.parametrize('returned_id', [None, '9007199254740993'])
def test_identity_owned_by_backend_and_few_shot_stream_protocol(store, tmp_path, role, returned_id):
    uid = '9007199254740993301'
    store.save_settings({**store.settings(), 'analysis_mode': 'direct' if role == 'analysis' else 'two_stage'})
    store.ingest([event(1, uid=uid)])
    job = store.claim(role)
    original = copy.deepcopy(job)
    output = precise(job) if role == 'analysis' else screen(job)
    wire = [{k: v for k, v in r.items() if k != 'user_id'} for r in output]
    if returned_id is not None:
        wire[0]['user_id'] = returned_id
    text = json.dumps({'results': wire}, ensure_ascii=False)
    log = RequestLog(tmp_path / 'requests')

    def handler(request):
        body = json.loads(request.content)
        assert body['stream'] is True
        assert uid not in request.content.decode() and 'user_id' not in request.content.decode()
        assert len(body['messages']) == 6
        assert [m['role'] for m in body['messages']] == ['system', 'user', 'assistant', 'user', 'assistant', 'user']
        assert len(json.loads(body['messages'][-1]['content'])['viewers']) == 1
        encoded = frame(delta(content=text[:20])) + frame(delta(content=text[20:], finish='stop')) + frame('[DONE]')
        index = encoded.index('合'.encode()) + 1 if '合'.encode() in encoded else 35
        return response([b': heartbeat\n\n', frame(delta(content='')), frame(delta(reasoning='private-reasoning')),
                         encoded[:index], encoded[index:]])

    result = asyncio.run(ModelClient(httpx.MockTransport(handler), log).analyze(job, store.model(role)))
    assert result == output and job == original
    assert store.complete(job, result)
    rows = [json.loads(line) for line in log.path.read_text(encoding='utf-8').splitlines()]
    record = next(row for row in rows if row['event'] == 'request_finished')
    assert record['response_mode'] == 'sse' and record['finish_reason'] == 'stop'
    assert record['first_reasoning_seconds'] <= record['first_content_seconds']
    assert record['total_deadline_seconds'] == 180
    assert record['content_events'] == 2 and record['reasoning_chars'] > 0
    assert 'private-reasoning' not in log.path.read_text(encoding='utf-8')
    assert bool(record.get('ignored_model_identity')) == (returned_id is not None)


@pytest.mark.parametrize('case,code', [
    ('length', 'model_response_incomplete'), ('missing_done', 'model_response_incomplete'),
    ('missing_stop', 'model_response_incomplete'), ('refusal', 'model_response_incomplete'),
    ('error_429', 'model_http_429'), ('error_busy', 'model_stream_error'),
    ('bad_event', 'model_invalid_json'), ('bad_content', 'model_invalid_json'),
    ('extra_choice', 'model_invalid_json'), ('foreign_evidence', 'model_invalid_evidence'),
])
def test_stream_errors_never_settle_comments(store, case, code):
    store.ingest([event(1)])
    job = store.claim('screening')
    values = screen(job)
    if case == 'foreign_evidence':
        values[0]['evidence_event_ids'] = ['unknown']
    content = model_response(values)['choices'][0]['message']['content']
    final = delta(content=content, finish='length' if case == 'length' else 'stop')
    if case == 'missing_stop':
        final['choices'][0]['finish_reason'] = None
    if case == 'refusal':
        final['choices'][0]['delta']['refusal'] = 'refused'
    if case == 'bad_content':
        final['choices'][0]['delta']['content'] = []
    if case == 'extra_choice':
        final['choices'].append(delta(content='other')['choices'][0])
    chunks = [frame(final)]
    if case.startswith('error_'):
        chunks.append(frame({'error': {'code': 429 if case == 'error_429' else 'busy'}}))
    if case == 'bad_event':
        chunks.append(frame('{bad'))
    if case != 'missing_done':
        chunks.append(frame('[DONE]'))
    with pytest.raises(ModelError, match=code):
        asyncio.run(ModelClient(httpx.MockTransport(lambda request: response(chunks))).analyze(job, store.model('screening')))
    assert store.status()['screening'] == {'processing': 1}


@pytest.mark.parametrize('activity', ['heartbeat', 'reasoning', 'content', 'headers'])
def test_absolute_budget_does_not_reset_with_activity(store, tmp_path, activity):
    store.ingest([event(1)])
    job = store.claim('screening')
    log = RequestLog(tmp_path / 'requests')

    class Endless(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            while True:
                await asyncio.sleep(0.005)
                yield b': heartbeat\n\n' if activity == 'heartbeat' else frame(delta(
                    reasoning='ongoing' if activity == 'reasoning' else None,
                    content='x' if activity == 'content' else None))

        async def aclose(self):
            self.closed = True

    stream = Endless()

    async def handler(request):
        if activity == 'headers':
            await asyncio.Event().wait()
        return httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=stream)

    client = ModelClient(httpx.MockTransport(handler), log)
    started = time.perf_counter()
    with pytest.raises(ModelError, match='model_timeout'):
        asyncio.run(client.analyze(job, {**store.model('screening'), 'timeout_seconds': 0.05}))
    assert time.perf_counter() - started < 2
    record = next(json.loads(line) for line in log.path.read_text(encoding='utf-8').splitlines()
                  if json.loads(line)['event'] == 'request_finished')
    assert record['total_deadline_exceeded'] is True
    assert stream.closed or activity == 'headers'


def test_few_shot_schema_and_config_default():
    assert ModelSettings().timeout_seconds == 180
    for schema in (SingleScreening, SingleAnalysis):
        examples = few_shot(schema)
        assert len(examples) == 4
        for message in examples[1::2]:
            schema.model_validate_json(message['content'])


@pytest.mark.parametrize('field', ['industry', 'ai_level', 'team', 'current_system'])
def test_missing_profile_field_is_unknown_without_regenerating_answer(store, tmp_path, field):
    store.save_settings({**store.settings(), 'analysis_mode': 'direct'})
    store.ingest([event(1)])
    job = store.claim('analysis')
    output = precise(job)
    output[0].pop(field)
    log = RequestLog(tmp_path / 'profile-log')
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=model_response(output))
    result = asyncio.run(ModelClient(httpx.MockTransport(handler), log).analyze(job, store.model('analysis')))
    assert len(calls) == 1 and result[0][field] == '未知'
    assert store.complete(job, result)
    records = [json.loads(line) for line in log.path.read_text(encoding='utf-8').splitlines()]
    record = next(r for r in records if r['event'] == 'request_finished')
    assert record['missing_profile_fields'] == [field]


@pytest.mark.parametrize('field', ['intent_score', 'suggested_reply', 'evidence_event_ids', 'needs_reply'])
def test_missing_critical_analysis_field_still_fails(store, field):
    store.save_settings({**store.settings(), 'analysis_mode': 'direct'})
    store.ingest([event(1)])
    job = store.claim('analysis')
    output = precise(job)
    output[0].pop(field)
    with pytest.raises(ModelError, match='model_invalid_json'):
        asyncio.run(ModelClient(httpx.MockTransport(lambda r: httpx.Response(200, json=model_response(output))))
                    .analyze(job, store.model('analysis')))


def test_invalid_profile_value_is_not_silently_replaced(store):
    store.save_settings({**store.settings(), 'analysis_mode': 'direct'})
    store.ingest([event(1)])
    job = store.claim('analysis')
    output = precise(job)
    output[0]['team'] = {'unexpected': True}
    with pytest.raises(ModelError, match='model_invalid_json'):
        asyncio.run(ModelClient(httpx.MockTransport(lambda r: httpx.Response(200, json=model_response(output))))
                    .analyze(job, store.model('analysis')))


def test_sse_multiline_data_and_limit(store):
    store.ingest([event(1)])
    job = store.claim('screening')
    content = model_response(screen(job))['choices'][0]['message']['content']
    event_json = json.dumps(delta(content=content, finish='stop'))
    first, rest = event_json.split(':', 1)
    multiline = ('data: ' + first + ':\ndata: ' + rest + '\n\n').encode()
    client = ModelClient(httpx.MockTransport(lambda request: response([multiline, frame('[DONE]')])))
    assert asyncio.run(client.analyze(job, store.model('screening'))) == screen(job)
    oversized = ModelClient(httpx.MockTransport(lambda request: response([b'x' * 2000001])))
    with pytest.raises(ModelError, match='model_response_too_large'):
        asyncio.run(oversized.analyze(job, store.model('screening')))


def test_inflight_new_comment_and_stream_retry_keep_original_snapshot(store, tmp_path):
    store.save_settings({**store.settings(), 'analysis_mode': 'direct'})
    store.ingest([event(1)])
    seen = []

    def handler(request):
        payload = json.loads(json.loads(request.content)['messages'][-1]['content'])
        seen.append(payload)
        if len(seen) == 1:
            store.ingest([event(2)])
            return response([frame(delta(content='{'))])
        value = precise({'payload': payload})
        content = model_response(value)['choices'][0]['message']['content']
        return response([frame(delta(content=content, finish='stop')), frame('[DONE]')])

    worker = Workers(store, lambda: tmp_path / 'missing')
    worker.client.transport = httpx.MockTransport(handler)
    assert asyncio.run(worker.run_once('analysis'))
    with store.connect(True) as db:
        db.execute('UPDATE jobs SET retry_at=0')
    assert asyncio.run(worker.run_once('analysis'))
    attempts = [json.loads(line) for line in worker.request_log.path.read_text(encoding='utf-8').splitlines()
                if json.loads(line)['event'] == 'job_attempt_finished']
    assert attempts[-1]['status'] == 'completed', attempts
    assert seen[0] == seen[1]
    assert len(seen[1]['viewers'][0]['comments']) == 1
    assert store.status()['screening'] == {'done': 1, 'pending': 1}
    assert asyncio.run(worker.run_once('analysis'))
    result = store.viewer_analysis(ROOM, event(1)['user_id'], DAY)['result']
    assert len(result['coverage']) == 2


def test_cancel_stream_releases_account_and_connection_without_partial_result(store, tmp_path):
    store.save_settings({**store.settings(), 'analysis_mode': 'direct'})
    store.ingest([event(1)])
    worker = Workers(store, lambda: tmp_path / 'missing')

    async def run():
        received = asyncio.Event()

        class Pending(httpx.AsyncByteStream):
            closed = False

            async def __aiter__(self):
                yield frame(delta(content='{'))
                received.set()
                await asyncio.Event().wait()

            async def aclose(self):
                self.closed = True

        stream = Pending()
        worker.client.transport = httpx.MockTransport(lambda request: httpx.Response(
            200, headers={'content-type': 'text/event-stream'}, stream=stream))
        task = asyncio.create_task(worker.run_once('analysis'))
        await asyncio.wait_for(received.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stream.closed and not any(store.pool.active.values())
        assert not worker.active_jobs

    asyncio.run(run())
    with store.connect() as db:
        assert db.execute('SELECT count(*) FROM results').fetchone()[0] == 0
        assert db.execute('SELECT status FROM jobs').fetchone()[0] == 'running'
    store.recover()
    retry = store.claim('analysis')
    assert retry['attempts'] == 2
    assert [c['event_id'] for c in retry['payload']['viewers'][0]['comments']] == ['synthetic-1']

import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from assistant_app.api import create_app
from assistant_app.config import GeneralSettings, ModelSettings
from assistant_app.models import ModelClient, ModelError
from .support import DAY, ROOM, IsolatedCollector, event, model_response, precise, screen


@pytest.mark.parametrize('problem,code', [
    ('omit', 'model_invalid_json'), ('unexpected_field', 'model_invalid_json'),
    ('foreign_evidence', 'model_invalid_evidence'), ('wrong_targets', 'model_target_mismatch'),
    ('truncated', 'model_response_incomplete'), ('malformed', 'model_invalid_json'),
    ('401', 'model_http_401'), ('429', 'model_http_429'), ('redirect', 'model_http_302'),
])
def test_invalid_provider_output_keeps_comments_unsettled(store, problem, code):
    store.ingest([event(1)])
    job = store.claim('screening')
    results = screen(job)
    if problem == 'omit':
        results = []
    if problem == 'unexpected_field':
        results[0]['unrecognized_field'] = 'other'
    if problem == 'foreign_evidence':
        results[0]['evidence_event_ids'] = ['unknown-message']
    if problem == 'wrong_targets':
        results[0]['target_event_ids'] = []

    def handler(request):
        if problem in ('401', '429', 'redirect'):
            return httpx.Response(302 if problem == 'redirect' else int(problem))
        if problem == 'malformed':
            return httpx.Response(200, text='not json')
        return httpx.Response(200, json=model_response(results, 'length' if problem == 'truncated' else 'stop'))

    client = ModelClient(httpx.MockTransport(handler))
    with pytest.raises(ModelError, match=code):
        asyncio.run(client.analyze(job, store.model('screening')))
    assert store.status()['screening'] == {'processing': 1}


def test_model_http_contract_is_single_viewer(store):
    store.ingest([event(1, uid='a'), event(2, uid='b')])
    job = store.claim('screening')
    outputs = screen(job)

    def handler(request):
        body = json.loads(request.content)
        assert str(request.url) == 'https://test.invalid/v1/chat/completions'
        assert body['response_format'] == {'type': 'json_object'}
        assert body['model'] == 'synthetic-fixture'
        assert body['stream'] is True and 'max_tokens' in body
        viewers = json.loads(body['messages'][-1]['content'])['viewers']
        assert len(viewers) == 1 and 'user_id' not in viewers[0]
        return httpx.Response(200, json=model_response(list(reversed(outputs))))

    result = asyncio.run(ModelClient(httpx.MockTransport(handler)).analyze(job, store.model('screening')))
    assert [r['user_id'] for r in result] == ['a']
    store.complete(job, result)
    assert store.claim('screening')['payload']['viewers'][0]['user_id'] == 'b'


def test_long_history_summary_has_evidence_and_original_coverage(store):
    store.ingest([event(1, content='人工客服问题。' * 3000)])
    job = store.claim('screening')
    store.complete(job, screen(job))
    job = store.claim('analysis')
    calls = []

    def handler(request):
        body = json.loads(request.content)
        payload = json.loads(body['messages'][-1]['content'])
        assert 'user_id' not in request.content.decode()
        assert event(1)['user_id'] not in request.content.decode()
        calls.append(payload)
        if 'comments' in payload:
            value = {'facts': [{'text': '希望自动化客服', 'evidence_event_ids': ['synthetic-1']}]}
        else:
            assert payload['viewers'][0]['coverage_count'] == 1
            value = {'results': [{k: v for k, v in r.items() if k != 'user_id'} for r in precise(job)]}
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(value)}}]})

    config = {**store.model('analysis'), 'input_budget': 12000}
    result = asyncio.run(ModelClient(httpx.MockTransport(handler)).analyze(job, config))
    assert len(calls) > 2
    assert store.complete(job, result)
    assert store.viewer_analysis(ROOM, event(1)['user_id'], DAY)['result']['coverage'] == ['synthetic-1']


@pytest.fixture
def local_app(tmp_path):
    collector = IsolatedCollector(tmp_path)
    app = create_app(tmp_path, collector, background=False)
    with TestClient(app) as client:
        yield app, client, collector


def test_api_gets_never_trigger_models_and_keys_are_redacted(local_app):
    app, client, collector = local_app
    headers = {'X-Live-Assistant': '1'}
    value = ModelSettings(base_url='https://test.invalid/v1', model='screen-model', api_key='fixture-secret-only').model_dump()
    r = client.put('/api/settings/models/screening', json=value, headers=headers)
    assert r.status_code == 200 and 'fixture-secret-only' not in r.text
    assert r.json()['key_configured']
    assert client.get('/api/settings').json()['models']['analysis']['model'] == ''
    app.state.store.ingest([event(1)])
    for path in ('/api/status', '/api/settings', f'/api/viewers?start={DAY}',
                 f'/api/viewers/{event(1)["user_id"]}/comments?start={DAY}',
                 f'/api/viewers/{event(1)["user_id"]}/analysis?day={DAY}'):
        result = client.get(path)
        assert result.status_code == 200
        assert 'fixture-secret-only' not in result.text
    assert app.state.store.status()['jobs'] == []
    invalid = client.put('/api/settings/models/screening', json={**value, 'timeout_seconds': 'fixture-secret-only'}, headers=headers)
    assert invalid.status_code == 422 and 'fixture-secret-only' not in invalid.text
    value['api_key'] = ''
    client.put('/api/settings/models/screening', json=value, headers=headers)
    assert app.state.store.model('screening')['api_key'] == 'fixture-secret-only'
    client.put('/api/settings/models/screening', json={**value, 'clear_key': True}, headers=headers)
    assert app.state.store.model('screening')['api_key'] == ''


@pytest.mark.parametrize('headers', [{}, {'X-Live-Assistant': '1', 'Origin': 'https://attacker.invalid'},
                                    {'X-Live-Assistant': '1', 'Sec-Fetch-Site': 'cross-site'}])
def test_api_rejects_untrusted_writes(local_app, headers):
    app, client, _ = local_app
    assert client.put('/api/settings/general', headers=headers, json=GeneralSettings().model_dump()).status_code == 403


def test_api_invalid_host_dates_cursor_and_live_room_conflict(local_app):
    app, client, collector = local_app
    assert client.get('/api/status', headers={'Host': 'attacker.invalid'}).status_code == 403
    assert client.get('/api/viewers?start=invalid').status_code == 422
    assert client.get('/api/viewers?start=2026-09-08&end=2026-09-07').status_code == 422
    for cursor in ('[NaN,1]', '[1,1.5]', '[1,-2]', '{"a":1}'):
        assert client.get('/api/viewers/u/comments', params={'before': cursor}).status_code == 422
    collector.running = True
    before = app.state.store.settings()
    response = client.put('/api/settings/general', json={**before, 'room_url': 'https://live.douyin.com/123'},
                          headers={'X-Live-Assistant': '1'})
    assert response.status_code == 409
    assert app.state.store.settings() == before


@pytest.mark.parametrize('url', ['http://remote.invalid/v1', 'https://user:pass@test.invalid/v1',
                                 'https://test.invalid/v1?api_key=x', 'https://test.invalid/v1/chat/completions'])
def test_model_base_url_validation(url):
    with pytest.raises(ValueError):
        ModelSettings(base_url=url)

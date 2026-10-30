import json
import time
from datetime import datetime, timezone
from pathlib import Path

from assistant_app.config import CollectorSettings, GeneralSettings, default_model
from assistant_app.models import ModelClient

ROOM = GeneralSettings().room_url
DAY = '2026-09-07'
MOMENT = datetime.fromisoformat(DAY + 'T12:00:00+08:00').timestamp()


def event(seq, uid='9007199254740993123', content='我们想做客服自动化', nickname='测试观众', at=None, scene='scene-1'):
    at = MOMENT + seq if at is None else at
    return {'event_id': f'synthetic-{seq}', 'document_id': 'synthetic', 'seq': seq,
            'room_url': ROOM, 'user_id': uid, 'nickname': nickname, 'content': content,
            'user_id_kind': 'platform', 'platform_message_id': str(8000000000000000000 + seq),
            'platform_room_id': scene, 'source_time_raw': str(at),
            'source_time_kind': 'payload.event_time_unix_seconds',
            'captured_at': datetime.fromtimestamp(at + 0.3, timezone.utc).isoformat(), 'phase': 'live'}


def configure(store):
    # Legacy regression fixtures explicitly exercise the preserved two-stage protocol.
    store.save_settings({**store.settings(), 'analysis_mode': 'two_stage'})
    for role in ('screening', 'analysis'):
        value = default_model(role).model_dump()
        value.update(base_url='https://test.invalid/v1', model='synthetic-fixture', input_budget=100000)
        store.save_model(role, value)


def screen(job, decision='analyze'):
    return [{'user_id': v.get('user_id', 'backend-bound'), 'target_event_ids': v['target_event_ids'],
             'decision': decision, 'reason': '合成测试判断', 'needs_reply': decision != 'skip',
             'evidence_event_ids': v['target_event_ids'][:1]} for v in job['payload']['viewers']]


def precise(job, score=4):
    return [{'user_id': v.get('user_id', 'backend-bound'), 'summary': '合成测试：观众希望减少人工客服工作。',
             'industry': '电商', 'ai_level': '入门', 'team': '5人', 'current_system': '未知',
             'current_need': '客服自动化，先评估订单查询和售后场景', 'intent_score': score,
             'intent_reason': '有明确业务场景，采购时间仍待确认', 'needs_reply': True,
             'priority_reply': True, 'suggested_reply': '可以先把重复咨询分类，选订单查询做一个小范围验证。',
             'followup_question': '目前订单数据在哪个系统里，是否提供接口？', 'lead_value': '值得进一步了解',
             'lead_approach': '邀请提供脱敏流程样例，沟通试点范围', 'risks': '系统接口和权限尚未确认',
             'evidence_event_ids': [v['comments'][-1]['event_id']]} for v in job['payload']['viewers']]


def model_response(results, finish='stop', include_identity=False):
    if not include_identity:
        results = [{k: v for k, v in result.items() if k != 'user_id'} for result in results]
    return {'choices': [{'finish_reason': finish, 'message': {'content': json.dumps({'results': results}, ensure_ascii=False)}}]}


class FixtureClient:
    async def analyze(self, job, config):
        return screen(job) if job['role'] == 'screening' else precise(job)


class IsolatedCollector:
    """No live browser, source import or collector launch is possible in this fixture."""
    def __init__(self, directory):
        self.path = Path(directory) / 'collector-fixture.json'
        self.value = {'room_url': ROOM, **CollectorSettings().model_dump()}
        if self.path.exists():
            self.value = json.loads(self.path.read_text(encoding='utf-8'))
        self.running = False

    def settings(self):
        return dict(self.value)

    def source(self):
        return self.path.parent / 'synthetic-source.sqlite3'

    def status(self):
        return {'status': 'isolated_test', 'process_running': self.running}

    def save(self, value):
        if self.running and value['room_url'] != self.value['room_url']:
            raise ValueError('collector_stop_before_room_change')
        self.value = dict(value)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(value), encoding='utf-8')

    def start(self):
        raise RuntimeError('isolated_test_collector_disabled')

    def stop(self):
        return {'status': 'isolated_test'}

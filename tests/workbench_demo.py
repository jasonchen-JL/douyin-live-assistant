"""BR-16 offline workbench: isolated SQLite and local SSE mock, never real provider calls."""
import argparse
import asyncio
import json
import time

import httpx
import uvicorn
from pydantic import BaseModel, Field

from assistant_app.api import create_app
from assistant_app.config import ROOT
from assistant_app.models import ModelClient
from assistant_app.store import Store
from .support import IsolatedCollector, configure, event
from .test_markdown import activate, result


def text_for(nickname, latest, count):
    return (f'**主播直接说：** 这件事现在主要是谁在负责？\n\n'
            f'**观众背景与需求**\n{nickname}提到：{latest}\n\n'
            '**还没问清楚**\n- 每天需要处理多少次\n- 目前使用什么工具\n\n'
            '**为什么先问这一句**\n先了解目前怎么做，再确定哪些重复工作值得自动化。\n\n'
            '**下一步怎么接**\n如果主要靠人工，再问每天处理量；如果已有软件，再确认软件能提供哪些数据。\n\n'
            '**进一步沟通**\n先确认业务量和现有流程，不急着留联系方式。\n\n'
            f'模拟结果 · 上下文 {count} 条')


def seed(directory):
    if (directory / 'assistant.sqlite3').exists():
        return
    store = Store(directory / 'assistant.sqlite3')
    configure(store)
    activate(store)
    store.save_settings({**store.settings(), 'topic': '企业 AI · 业务流程与客服自动化'})
    now = time.time()
    phrases = ['我是做跨境电商的', '现在8个人', '就是客服', '每天几百条咨询', '订单查询特别多', '想先试一下自动化']
    records = [event(i, uid='demo-a', nickname='跨境老王', content=phrases[(i - 1) % 6],
                     at=now - (86400 if i <= 15 else 1300) + i, scene='scene-1' if i <= 15 else 'scene-2') for i in range(1, 121)]
    records += [event(200 + i, uid='demo-b', nickname='连锁门店小陈', at=now - 80 + i,
                      content=content) for i, content in enumerate(['我们有30多家店', '员工回答不太统一', '想先做员工培训', '对', '有现成资料', '主要在微信上问', '总部三个人负责'])]
    records += [event(300, uid='demo-c', nickname='刚入门的阿明', content='Agent能做什么？', at=now - 40),
                event(301, uid='demo-d', nickname='同名观众', content='工厂想了解排产', at=now - 60),
                event(302, uid='demo-e', nickname='同名观众', content='我做物流，想查运单', at=now - 50),
                event(303, uid='demo-long', nickname='长昵称边界ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789',
                      content='接口名称' + 'OrderManagementSystem' * 8, at=now - 70)]
    store.ingest(records)
    while job := store.claim('analysis'):
        viewer = job['payload']['viewers'][0]
        store.complete(job, result(job, text_for(viewer['nickname'], viewer['comments'][-1]['content'], len(viewer['comments']))))


class CommentCommand(BaseModel):
    uid: str = 'demo-a'
    content: str = Field(min_length=1, max_length=2000)


class ModelCommand(BaseModel):
    delay: float = Field(0.5, ge=0, le=45)
    fail: bool = False


def application(directory):
    seed(directory)
    app = create_app(directory, IsolatedCollector(directory), background=True)
    control = {'delay': 0.5, 'fail': False, 'calls': 0}

    async def respond(request):
        assert request.url.host == 'test.invalid'
        payload = json.loads(json.loads(request.content)['messages'][-1]['content'])
        control['calls'] += 1
        delay, fail = control['delay'], control['fail']
        text = text_for(payload['nickname'], payload['comments'][-1]['content'], len(payload['comments']))
        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                await asyncio.sleep(delay)
                for part in (text[:55], text[55:]):
                    value = {'choices': [{'delta': {'content': part}, 'finish_reason': None}]}
                    yield ('data: ' + json.dumps(value, ensure_ascii=False) + '\n\n').encode()
                    await asyncio.sleep(0.25)
                yield ('data: ' + json.dumps({'choices': [{'delta': {}, 'finish_reason': 'length' if fail else 'stop'}]}) + '\n\n').encode()
                yield b'data: [DONE]\n\n'
        return httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=Stream())

    app.state.workers.client = ModelClient(httpx.MockTransport(respond), request_log=app.state.workers.request_log)

    @app.post('/test/comment')
    def append(command: CommentCommand):
        store = app.state.store
        with store.connect() as db:
            row = db.execute('SELECT nickname FROM comments WHERE uid=? ORDER BY id DESC LIMIT 1', (command.uid,)).fetchone()
        seq = time.time_ns()
        value = event(seq, uid=command.uid, nickname=row['nickname'] if row else '新增模拟观众', content=command.content, at=time.time())
        return {'inserted': store.ingest([value]), 'event_id': value['event_id']}

    @app.put('/test/model')
    def model(command: ModelCommand):
        control.update(command.model_dump())
        return dict(control)

    @app.get('/test/state')
    def state():
        return {**control, **app.state.store.status()}

    return app


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8768)
    parser.add_argument('--directory', default='runtime/workbench-demo-20260909')
    args = parser.parse_args()
    directory = (ROOT / args.directory).resolve()
    if not directory.is_relative_to(ROOT / 'runtime') or directory == ROOT / 'runtime':
        parser.error('Use a dedicated runtime subdirectory')
    uvicorn.run(application(directory), host='127.0.0.1', port=args.port, access_log=False)

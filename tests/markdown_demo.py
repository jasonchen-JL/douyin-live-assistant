"""Isolated browser acceptance fixture. No live collector or external HTTP."""
import asyncio
import time

import httpx
import uvicorn

from assistant_app.api import create_app
from assistant_app.config import ROOT
from assistant_app.models import ModelClient
from .support import IsolatedCollector, configure, event, precise
from .test_streaming import delta, frame

DIRECTORY = ROOT / 'runtime' / 'markdown-ui-20260909'
TEXT = '''**主播直接说：** 每天大概有多少次订单查询？

**观众背景与需求**：电商团队，希望自动查询订单。

**还缺什么信息**：查询量和接口权限待确认。

**为什么先问这句**：先判断重复工作的规模。

**不同回答怎么接**：量大就问主要卡点；量小就问更频繁的重复任务。

**进一步沟通建议**：邀请提供脱敏流程样例，不承诺未经验证的交付能力。

<script>window.markdownInjected=true</script>
<img src="https://invalid.example/tracker" onerror="window.markdownInjected=true">
[危险链接](javascript:alert(1)) [普通链接](https://invalid.example/)
'''


def create_fixture():
    if (DIRECTORY / 'assistant.sqlite3').exists():
        raise RuntimeError('Fixture already exists; do not overwrite')
    app = create_app(DIRECTORY, IsolatedCollector(DIRECTORY), background=False)
    store, worker = app.state.store, app.state.workers
    configure(store)
    store.save_settings({**store.settings(), 'analysis_mode': 'direct'})
    store.ingest([event(1, uid='legacy', nickname='模拟旧版观众', at=time.time() - 10)])
    job = store.claim('analysis')
    store.complete(job, precise(job))
    store.save_settings({**store.settings(), 'analysis_mode': 'markdown_direct'})
    store.ingest([event(2, uid='markdown', nickname='模拟Markdown观众', at=time.time())])
    job = store.claim('analysis')
    store.complete(job, [{'user_id': 'markdown', 'format': 'markdown_v1', 'analysis_markdown': TEXT,
                          'intent_score': None, 'needs_reply': None}])
    release = asyncio.Event()
    finish = 'stop'

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield frame(delta(reasoning='never-show-this-reasoning'))
            yield frame(delta(content='**主播直接说：** 当前最耗时的是哪个步骤？\n\n'))
            await release.wait()
            yield frame(delta(content='**观众背景与需求**：这是更新后的测试提词。', finish=finish))
            yield frame('[DONE]')

    worker.client = ModelClient(httpx.MockTransport(lambda r: httpx.Response(
        200, headers={'content-type': 'text/event-stream'}, stream=Stream())))

    @app.post('/fixture/start')
    async def start(fail: bool = False):
        nonlocal finish
        if worker.running_tasks:
            return {'started': False}
        release.clear()
        finish = 'length' if fail else 'stop'
        store.ingest([event(100 + store.status()['comments'], uid='markdown', nickname='模拟Markdown观众', at=time.time())])
        task = asyncio.create_task(worker.run_once('analysis'))
        worker.running_tasks.add(task)
        task.add_done_callback(worker.running_tasks.discard)
        return {'started': True}

    @app.post('/fixture/release')
    async def complete():
        release.set()
        return {'released': True}

    return app


if __name__ == '__main__':
    uvicorn.run(create_fixture(), host='127.0.0.1', port=8768, access_log=False)

"""Offline synthetic SQLite fixture. Never starts the live collector or an external model."""
import argparse
import asyncio
import json
import time

import uvicorn

from assistant_app.api import create_app
from assistant_app.config import ROOT
from assistant_app.store import Store
from assistant_app.workers import Workers
from .support import FixtureClient, IsolatedCollector, configure, event


def seed(directory):
    if (directory / 'assistant.sqlite3').exists():
        raise RuntimeError('Demo already exists; select a new directory')
    store = Store(directory / 'assistant.sqlite3')
    configure(store)
    store.save_settings({**store.settings(), 'topic': '模拟测试：企业 AI 客服与订单自动化',
                         'company_capabilities': '订单 API 对接、知识库与自动化试点'})
    now = time.time()
    records = [event(i, uid='demo-buyer', nickname='模拟电商负责人' if i > 10 else '模拟旧昵称',
                     content=['每天订单查询很多，希望减少客服重复回复', '团队5个人，ERP有接口',
                              '想先从订单查询做试点，预算两万元'][i % 3],
                     at=now - 1200 + i * 7, scene='scene-1' if i < 50 else 'scene-2') for i in range(1, 121)]
    records += [event(200 + i, uid=f'demo-{i}', nickname=nickname, content=content, at=now - 60 + i)
                for i, (nickname, content) in enumerate([
                    ('模拟技术咨询', 'RAG和微调有什么区别？'), ('模拟短句观众', '123'),
                    ('模拟同名观众', '有ERP接口，订单查询能自动化吗？'), ('模拟同名观众', '还在了解阶段'),
                    ('模拟工厂老板', '排产需要和MES连接，是否先评估接口？'),
                    ('模拟超长昵称用于边界检查ABCDEFGHIJKLMNOPQRSTUVWXYZ', '先了解一下')])]
    store.ingest(records)
    workers = Workers(store, lambda: directory / 'missing', FixtureClient())

    async def settle():
        while await workers.run_once('screening'):
            pass
        while await workers.run_once('analysis'):
            pass
    asyncio.run(settle())
    store.ingest([event(500, uid='demo-unscored', nickname='模拟未分析观众', content='刚到直播间', at=now)])
    store.save_settings({**store.settings(), 'analysis_enabled': False})
    print(json.dumps(store.status(), ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=('seed', 'serve', 'append'))
    parser.add_argument('--port', type=int, default=8766)
    parser.add_argument('--directory', default='runtime/validation-ui')
    args = parser.parse_args()
    directory = (ROOT / args.directory).resolve()
    if not directory.is_relative_to(ROOT / 'runtime') or directory == ROOT / 'runtime':
        parser.error('Use a dedicated runtime subdirectory')
    if args.command == 'seed':
        seed(directory)
    elif args.command == 'append':
        store = Store(directory / 'assistant.sqlite3')
        seq = 1000 + store.status()['comments']
        count = store.ingest([event(seq, uid='demo-buyer', nickname='模拟电商负责人',
                                    content='模拟实时新增：我们需要私有化部署', at=time.time())])
        print(json.dumps({'inserted': count}))
    else:
        uvicorn.run(create_app(directory, IsolatedCollector(directory), background=False),
                    host='127.0.0.1', port=args.port, access_log=False)


if __name__ == '__main__':
    main()

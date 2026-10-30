import json
import math
import os
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .collector import Collector, SessionLock
from .credentials import CredentialError
from .config import CollectorSettings, GeneralSettings, ModelSettings, PoolEntrySettings, ROOT, today
from .store import Store
from .workers import Workers


class AnalysisCommand(BaseModel):
    request_id: UUID
    room_url: str = Field(min_length=1, max_length=2048)


def create_app(data_dir=None, collector=None, background=True):
    data_dir = Path(data_dir or os.environ.get('DOUYIN_ASSISTANT_DATA', ROOT / 'runtime'))
    collector = collector or Collector()
    fresh = not (data_dir / 'assistant.sqlite3').exists()
    store = Store(data_dir / 'assistant.sqlite3')
    if fresh:
        store.save_settings({**store.settings(), 'room_url': collector.settings()['room_url']})
    workers = Workers(store, collector.source)

    @asynccontextmanager
    async def lifespan(app):
        # This reuses the proven OS lock, without locking or changing live browser profiles.
        with SessionLock(data_dir / 'web-service'):
            if background:
                workers.start()
            yield
            await workers.stop()

    app = FastAPI(title='Live Assistant', lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store, app.state.workers = store, workers

    @app.middleware('http')
    async def local_only(request: Request, call_next):
        try:
            host = urlsplit('http://' + request.headers.get('host', '')).hostname
        except ValueError:
            host = None
        if host not in ('127.0.0.1', 'localhost', '::1', 'testserver'):
            return JSONResponse({'error': 'invalid_host'}, status_code=403)
        if request.headers.get('sec-fetch-site') == 'cross-site':
            return JSONResponse({'error': 'cross_origin_denied'}, status_code=403)
        if request.method not in ('GET', 'HEAD'):
            origin = request.headers.get('origin')
            if origin and origin.rstrip('/') != str(request.base_url).rstrip('/'):
                return JSONResponse({'error': 'cross_origin_denied'}, status_code=403)
            if request.headers.get('x-live-assistant') != '1':
                return JSONResponse({'error': 'local_command_header_required'}, status_code=403)
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        if request.url.path.startswith('/api/'):
            response.headers['Cache-Control'] = 'no-store'
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, error):
        # FastAPI's default validation response can echo a submitted API key.
        return JSONResponse({'error': 'invalid_parameters'}, status_code=422)

    @app.exception_handler(CredentialError)
    async def credential_error(request, error):
        return JSONResponse({'error': str(error)}, status_code=503)

    def interval(start, end):
        a, b = start or today(), end or start or today()
        try:
            if date.fromisoformat(a) > date.fromisoformat(b):
                raise ValueError()
        except ValueError:
            raise HTTPException(422, 'invalid_date_range') from None
        return a, b

    @app.get('/api/settings')
    def settings():
        return {'general': store.settings(), 'models': {r: store.model(r, public=True) for r in ('screening', 'analysis')},
                'collector': collector.settings(), 'key_pool': store.pool.entries()}

    @app.put('/api/settings/general')
    def general(value: GeneralSettings):
        current = collector.settings()
        if current['room_url'] != value.room_url:
            try:
                collector.save({**current, 'room_url': value.room_url})
            except ValueError:
                raise HTTPException(409, 'collector_stop_before_room_change') from None
        store.save_settings(value.model_dump())
        return {'saved': True}

    @app.put('/api/settings/models/{role}')
    def model(role: Literal['screening', 'analysis'], value: ModelSettings):
        try:
            return store.save_model(role, value.model_dump())
        except ValueError:
            raise HTTPException(422, 'pool_entry_not_found') from None

    @app.post('/api/settings/key-pool')
    def add_pool_entry(value: PoolEntrySettings):
        return store.pool.save(None, value.model_dump())

    @app.put('/api/settings/key-pool/{entry_id}')
    def update_pool_entry(entry_id: str, value: PoolEntrySettings):
        if not any(e['id'] == entry_id for e in store.pool.entries()):
            raise HTTPException(404, 'pool_entry_not_found')
        return store.pool.save(entry_id, value.model_dump())

    @app.post('/api/settings/key-pool/{entry_id}/reset')
    def reset_pool_entry(entry_id: str):
        try:
            store.pool.reset(entry_id)
        except ValueError:
            raise HTTPException(404, 'pool_entry_not_found') from None
        return {'saved': True}

    @app.put('/api/settings/collector')
    def collector_settings(value: CollectorSettings):
        try:
            collector.save({'room_url': store.settings()['room_url'], **value.model_dump()})
        except ValueError as error:
            raise HTTPException(409, str(error)) from None
        return {'saved': True}

    @app.get('/api/status')
    def status():
        try:
            collection = collector.status()
        except (ValueError, OSError):
            collection = {'status': 'config_error', 'process_running': False}
        source_exists = collector.source().exists()
        if collection.get('source_ready') and (not source_exists or workers.health['import_error']):
            collection.update(source_ready=False, source_state='recovering', source_reason='source_read_failed')
        return {**store.status(), 'application': 'douyin-live-assistant', 'workers': workers.health, 'collector': collection,
                'analysis_commands_enabled': background,
                'models': {r: store.model(r, public=True)['configured'] for r in ('screening', 'analysis')},
                'today': today(), 'source_exists': source_exists}

    @app.post('/api/collector/{command}')
    def control(command: Literal['start', 'stop']):
        try:
            return collector.start() if command == 'start' else collector.stop()
        except (ValueError, OSError, RuntimeError):
            raise HTTPException(409, 'collector_command_failed') from None

    @app.post('/api/jobs/{role}/retry')
    def retry(role: Literal['screening', 'analysis']):
        return {'queued': store.retry_failed(role)}

    @app.get('/api/viewers')
    def viewers(start: str | None = None, end: str | None = None, query: str = Query('', max_length=100),
                view: Literal['all', 'leads', 'reply'] = 'all', active: int = Query(0, ge=0, le=1440),
                stars: int = Query(3, ge=1, le=5), sort: Literal['time_desc', 'time_asc', 'stars_desc', 'stars_asc'] = 'time_desc',
                offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100)):
        start, end = interval(start, end)
        return store.list_viewers(store.settings()['room_url'], start, end, query.strip(), view, active, stars, sort, offset, limit)

    @app.get('/api/viewers/{uid}/comments')
    def comments(uid: str, start: str | None = None, end: str | None = None, before: str | None = None,
                 after: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100), scope: Literal['range', 'all'] = 'range'):
        start, end = interval(start, end)
        if scope == 'all':
            start, end = '0001-01-01', '9999-12-31'
        cursor = None
        if before:
            try:
                cursor = json.loads(before)
                if (after or not isinstance(cursor, list) or len(cursor) != 2
                        or not all(type(v) in (float, int) and math.isfinite(v) for v in cursor)
                        or type(cursor[1]) is not int or cursor[1] < 1):
                    raise ValueError()
            except (ValueError, TypeError):
                raise HTTPException(422, 'invalid_cursor') from None
        return store.history(store.settings()['room_url'], uid, start, end, cursor, after, limit)

    @app.get('/api/viewers/{uid}/analysis')
    def analysis(uid: str, day: str | None = None, scope: Literal['day', 'all'] = 'day'):
        start, _ = interval(day, day)
        room = store.settings()['room_url']
        result = store.viewer_analysis(room, uid, start if scope == 'day' else None)
        draft = next((d for d in list(workers.drafts.values()) if d['room'] == room and d['uid'] == uid and (scope == 'all' or d['day'] == start)), None)
        if draft and draft['generation'] > result.get('published_generation', 0):
            # A preview is never a saved result or proof that newer comments were processed.
            result['preview'] = {k: draft[k] for k in ('job_id', 'analysis_markdown', 'coverage_count', 'generation')}
        return result

    @app.post('/api/viewers/{uid}/analysis', status_code=202)
    def request_analysis(uid: str, command: AnalysisCommand):
        if not background:
            raise HTTPException(409, 'analysis_worker_not_running')
        try:
            return store.request_analysis(command.room_url, uid, str(command.request_id))
        except ValueError as error:
            raise HTTPException(404 if str(error) == 'viewer_not_found' else 409, str(error)) from None

    dist = ROOT / 'web/dist'
    if dist.exists():
        app.mount('/assets', StaticFiles(directory=dist / 'assets'), name='assets')

    @app.get('/')
    def index():
        if not (dist / 'index.html').exists():
            return JSONResponse({'error': 'frontend_build_required'}, status_code=503)
        return FileResponse(dist / 'index.html')

    return app

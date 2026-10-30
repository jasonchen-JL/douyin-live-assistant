from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

ROOT = Path(__file__).resolve().parent.parent
CHINA = timezone(timedelta(hours=8))


def today():
    return datetime.now(CHINA).date().isoformat()


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid')


class ModelSettings(StrictModel):
    base_url: str = ''
    model: str = Field(default='', max_length=200)
    api_key: str = Field(default='', max_length=4096, repr=False)
    clear_key: bool = False
    pool_ids: list[str] = Field(default_factory=list, max_length=100)
    timeout_seconds: int = Field(default=180, ge=5, le=300)
    input_budget: int = Field(default=12000, ge=4000, le=200000)
    output_limit: int = Field(default=2000, ge=500, le=16000)
    json_mode: Literal['json_object', 'json_schema', 'prompt'] = 'json_object'
    token_parameter: Literal['max_tokens', 'max_completion_tokens'] = 'max_tokens'

    @field_validator('base_url')
    @classmethod
    def url(cls, value):
        value = value.strip().rstrip('/')
        if not value:
            return value
        p = urlsplit(value)
        if (p.username or p.password or p.query or p.fragment or not p.hostname
                or p.scheme not in ('https', 'http')):
            raise ValueError('Invalid model endpoint')
        if p.scheme == 'http' and p.hostname not in ('localhost', '127.0.0.1', '::1'):
            raise ValueError('Remote model endpoints require HTTPS')
        if p.path.endswith('/chat/completions'):
            raise ValueError('Use the API base URL, without /chat/completions')
        return value

    @field_validator('pool_ids')
    @classmethod
    def unique_pool_ids(cls, value):
        if len(value) != len(set(value)) or any(not v or len(v) > 80 for v in value):
            raise ValueError('Invalid pool selection')
        return value


class PoolEntrySettings(StrictModel):
    label: str = Field(min_length=1, max_length=80)
    base_url: str
    api_key: str = Field(default='', max_length=4096, repr=False)
    enabled: bool = True
    max_concurrency: int = Field(default=1, ge=1, le=10)
    min_interval_seconds: float = Field(default=2, ge=0, le=3600)

    @field_validator('base_url')
    @classmethod
    def url(cls, value):
        value = ModelSettings.url(value)
        if not value:
            raise ValueError('Missing pool endpoint')
        return value + '/v1' if not urlsplit(value).path else value


class GeneralSettings(StrictModel):
    room_url: str = 'https://live.douyin.com/100000000001'
    topic: str = Field(default='', max_length=3000)
    framework: str = Field(default='', max_length=5000)
    company_capabilities: str = Field(default='', max_length=5000)
    analysis_enabled: bool = True
    analysis_mode: Literal['two_stage', 'direct', 'markdown_direct'] = 'markdown_direct'
    max_concurrent_requests: int = Field(default=6, ge=1, le=20)
    screening_history_comment_limit: int = Field(default=20, ge=0, le=200)
    screening_batch_limit: int = Field(default=20, ge=1, le=100)
    screening_viewer_limit: int = Field(default=10, ge=1, le=20)
    analysis_viewer_limit: int = Field(default=3, ge=1, le=10)
    screening_interval_seconds: float = Field(default=2, ge=0.5, le=30)
    ui_interval_seconds: float = Field(default=1, ge=0.5, le=10)
    display_min_stars: int = Field(default=3, ge=1, le=5)
    display_active_minutes: int = Field(default=20, ge=0, le=1440)

    @field_validator('room_url')
    @classmethod
    def room(cls, value):
        p = urlsplit(value.strip())
        room = p.path.strip('/')
        if p.scheme != 'https' or p.netloc != 'live.douyin.com' or not room.isascii() or not room.isdigit():
            raise ValueError('Invalid Douyin room URL')
        return 'https://live.douyin.com/' + room


class CollectorSettings(StrictModel):
    duration_seconds: float = Field(default=0, ge=0, le=604800)
    scan_interval_ms: int = Field(default=250, ge=50, le=2000)
    batch_interval_seconds: float = Field(default=1, ge=0.2, le=10)


def default_model(role):
    return ModelSettings(input_budget=24000, output_limit=6000) if role == 'analysis' else ModelSettings()

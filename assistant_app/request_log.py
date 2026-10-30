"""Bounded, local-only model diagnostics. Never persist authentication headers."""
import json
import re
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


SECRET_FIELDS = {'apikey', 'authorization', 'proxyauthorization', 'cookie', 'setcookie',
                 'password', 'secret', 'token', 'accesstoken', 'refreshtoken', 'credentialref'}
_LOCK = threading.Lock()


def safe_url(value):
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ''
        if ':' in host:
            host = '[' + host + ']'
        return urlunsplit((parsed.scheme, host + (':' + str(parsed.port) if parsed.port else ''),
                           parsed.path, '', ''))
    except ValueError:
        return '[invalid-url]'


def redact(value, secrets=()):
    if isinstance(value, dict):
        return {k: '[redacted]' if re.sub('[^a-z]', '', str(k).lower()) in SECRET_FIELDS
                else redact(v, secrets) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v, secrets) for v in value]
    if not isinstance(value, str):
        return value
    for secret in secrets:
        if secret:
            value = value.replace(secret, '[redacted]')
    # Model content and error bodies frequently contain nested JSON strings.
    if value.lstrip().startswith(('{', '[')):
        try:
            return json.dumps(redact(json.loads(value), secrets), ensure_ascii=False)
        except (ValueError, RecursionError):
            pass
    value = re.sub(r'sk-[A-Za-z0-9_-]+', '[redacted]', value)
    value = re.sub(r'(?i)\bBearer\s+[^\s"\x27,;]+', 'Bearer [redacted]', value)
    value = re.sub(r'https?://[^\s"\x27<>]+', lambda m: safe_url(m[0]), value)
    value = re.sub(r'(?i)\b(api[_-]?key|authorization|password|access[_-]?token|secret)\s*[:=]\s*[^\s,;]+',
                   r'\1=[redacted]', value)
    return value


def preview(value, secrets=(), limit=131072):
    safe = redact(value, secrets)
    text = safe if isinstance(safe, str) else json.dumps(safe, ensure_ascii=False)
    raw = text.encode('utf-8')
    return {'text': raw[:limit].decode('utf-8', errors='ignore'), 'truncated': len(raw) > limit,
            'redacted_bytes': len(raw)}


class RequestLog:
    def __init__(self, directory, max_bytes=8 * 1024 * 1024, backups=5):
        self.path = Path(directory) / 'requests.jsonl'
        self.max_bytes, self.backups = max_bytes, backups
        self.failures = 0

    def write(self, event, secrets=(), **fields):
        try:
            record = redact({'time': datetime.now(timezone.utc).isoformat(), 'event': event, **fields}, secrets)
            line = json.dumps(record, ensure_ascii=False, allow_nan=False) + '\n'
            with _LOCK:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                if self.path.exists() and self.path.stat().st_size + len(line.encode('utf-8')) > self.max_bytes:
                    for index in range(self.backups, 0, -1):
                        source = self.path if index == 1 else self.path.with_suffix('.jsonl.' + str(index - 1))
                        if source.exists():
                            source.replace(self.path.with_suffix('.jsonl.' + str(index)))
                with self.path.open('a', encoding='utf-8') as output:
                    output.write(line)
        except Exception:
            # Diagnostics must not turn a valid response into another billable retry.
            self.failures += 1
            if self.failures == 1:
                print('model_request_log_write_failed', file=sys.stderr)

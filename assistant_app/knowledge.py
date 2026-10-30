"""Small, versioned company reference; frozen once when a new job is claimed."""
import hashlib
from pathlib import Path

from .config import ROOT

DEFAULT_KNOWLEDGE_PATH = ROOT / 'docs' / 'knowledge' / 'company.md'
MAX_KNOWLEDGE_BYTES = 32000


def snapshot(path=DEFAULT_KNOWLEDGE_PATH):
    # None is an explicit opt-out for isolated comparisons, not a missing-file fallback.
    if path is None:
        return {'status': 'disabled'}
    try:
        with Path(path).open('rb') as source:
            raw = source.read(MAX_KNOWLEDGE_BYTES + 1)
        if len(raw) > MAX_KNOWLEDGE_BYTES:
            return {'status': 'unavailable', 'error': 'knowledge_too_large'}
        content = raw.decode('utf-8-sig').replace('\r\n', '\n').strip()
    except (OSError, UnicodeError):
        return {'status': 'unavailable', 'error': 'knowledge_read_failed'}
    if not content:
        return {'status': 'unavailable', 'error': 'knowledge_empty'}
    sha = hashlib.sha256(content.encode('utf-8')).hexdigest()
    return {'status': 'ready', 'version': 'sha256-' + sha[:12], 'sha256': sha, 'content': content}

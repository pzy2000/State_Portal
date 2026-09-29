"""Conservative provider-failure classification and durable recovery receipts."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from . import cost
from .run_context import provider_capacity_failure, safe_error_text


def classify_failure(error: BaseException) -> dict[str, Any]:
    """Retry only typed, accounted provider outages; ambiguous usage is terminal."""
    supplied = getattr(error, 'recovery', None)
    if isinstance(supplied, dict):
        return dict(supplied)
    chain = []
    current = error
    while current is not None and id(current) not in {id(e) for e in chain}:
        chain.append(current)
        current = current.__cause__ or current.__context__
    for item in chain:
        artifacts = getattr(item, 'artifacts', {}) or {}
        mode = artifacts.get('failure_mode')
        if isinstance(item, cost.UsageAccountingError) or getattr(item, 'usage_accounting_complete', True) is False or mode == 'usage_accounting_incomplete':
            return {'retryable': False, 'category': 'usage_unknown', 'error': safe_error_text(error)}
        status = artifacts.get('status_code', getattr(item, 'status_code', None))
        if mode in {'client_error', 'provider_capacity'} or provider_capacity_failure(item) is not None or status in {400, 401, 403, 404, 422}:
            return {'retryable': False, 'category': 'provider_rejected', 'error': safe_error_text(error)}
    for item in chain:
        artifacts = getattr(item, 'artifacts', {}) or {}
        mode = artifacts.get('failure_mode')
        status = artifacts.get('status_code', getattr(item, 'status_code', None))
        if mode == 'gateway_outage':
            return {'retryable': True, 'category': 'provider_transient', 'error': safe_error_text(error)}
        if status == 429 or isinstance(status, int) and 500 <= status < 600:
            return {'retryable': True, 'category': 'provider_transient', 'error': safe_error_text(error)}
        if isinstance(item, ConnectionError) or type(item).__name__ in {'ConnectError', 'ConnectTimeout', 'APIConnectionError'}:
            if not cost.error_may_have_generated(item):
                return {'retryable': True, 'category': 'provider_transient', 'error': safe_error_text(error)}
    return {'retryable': False, 'category': 'infrastructure_or_integrity', 'error': safe_error_text(error)}


def atomic_receipt(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name)
    try:
        with os.fdopen(fd, 'w') as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write('\n')
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)

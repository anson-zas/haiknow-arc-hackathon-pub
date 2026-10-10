"""Retry known operation failures, preserving the whole-run and business limits."""
import json
import subprocess
import time

from agents.model.visual_transport import TransportFailure
from core.validation import CheckFailure, remaining


class OperationTimeout(RuntimeError):
    pass


def retryable(error):
    if isinstance(error, (subprocess.TimeoutExpired, OperationTimeout)):
        return True
    if isinstance(error, TransportFailure):
        # The proxy uses its default HTTP 400 for a local socket failure too.
        # Do not generalize that exception to malformed/authenticated requests.
        return (error.retry_limit is not None or
                str(error) == 'coding: timeout or connection failure' or
                error.status in (408, 429, 502, 503, 504))
    return False


def checked_report(report):
    if report.get('source_integrity') is False:
        raise CheckFailure('Source changed during detached validation')
    error = str(report.get('error', ''))
    # Older dependency reports contain only this short diagnostic. Unknown
    # command timeouts are sent to the model; never blindly replay a hung test.
    installation = (report.get('failure_kind') == 'infrastructure' or
                    (not report.get('failure_kind') and error.startswith(
                        ('TimeoutExpired: npm ci ', 'TimeoutExpired: npm install '))))
    if not report['passed'] and error.startswith('TimeoutExpired:') and installation:
        raise OperationTimeout(error)
    return report


def recover(operation, call, evidence, deadline, *, integrity=lambda: None, redact=str):
    attempt = 0
    bounded_attempts = 0
    while True:
        remaining(deadline)
        integrity()
        try:
            return call(attempt)
        except Exception as exc:
            # Explicit deadline, cancellation, tampering, and unknown failures
            # are never converted to retries or successful delivery.
            integrity()
            if not retryable(exc):
                raise
            # A successful operation returns above; only consecutive errors count.
            # This is an error circuit, never a quota on successful model calls.
            if attempt >= 2:
                raise
            limit = getattr(exc, 'retry_limit', None)
            if limit is not None:
                if bounded_attempts >= limit:
                    print(f'[haiknow] protocol recovery exhausted for {operation}; limit {limit}', flush=True)
                    raise
                bounded_attempts += 1
            remaining(deadline)
            attempt += 1
            evidence.mkdir(parents=True, exist_ok=True)
            record = {'operation':operation, 'retry':attempt,
                      'error':redact(f'{type(exc).__name__}: {exc}')}
            with (evidence/'recovery.jsonl').open('a', encoding='utf-8') as handle:
                handle.write(json.dumps(record, ensure_ascii=False)+'\n')
            print(f'[haiknow] recovering {operation}; attempt {attempt}', flush=True)
            time.sleep(remaining(deadline, cap=min(30, attempt)))

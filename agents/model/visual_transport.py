"""Loopback model transport: scoped images and opt-in Chat coding conversion.

The SDK owns tool execution and conversation compaction. This bridge preserves
native Responses bytes or converts the configured coding alias to Chat, keeping
its tool-turn reasoning private in memory. No external listener or new tools.
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone
from hashlib import sha256
from http.client import HTTPException, IncompleteRead
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import secrets
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from arcbench_security import secure_url
from agents.model.visual_analysis import VISUAL_ANALYSIS_PROMPT_VERSION, build_visual_analysis_prompt
from core.references import images_enabled, parse_visual_facts


# Match the OpenAI client's default used by the working ARC reference.
# A whole-run deadline applies only when explicitly configured by the caller.
VISUAL_TIMEOUT = 600.0
MAX_REQUEST_BYTES = 32 * 1024 * 1024
PROXY_ERROR_MAX_BYTES = 65536
PROXY_RETRY_DELAYS = (0.5, 1.0)
PROXY_RESET_REASONS = {
    'connection reset by peer': 'http_400_proxy_connection_reset',
    'unexpected eof': 'http_400_proxy_unexpected_eof',
    'http2: client connection force closed via clientconn.close': 'http_400_proxy_http2_client_close',
}
CODING_TOOLS = frozenset({'exec_command', 'write_stdin', 'shell', 'shell_command',
                          'update_plan', 'view_image', 'apply_patch'})
_DEFAULT_IDLE = object()
_TIMING_FRAME_LIMIT = 256 * 1024
CHAT_HEARTBEAT_SECONDS = 5.0


class _ChatHeartbeat:
    """Keep the local SDK stream alive while synchronous upstream JSON waits.

    Only this thread writes until stop/join; final events are emitted afterwards.
    A cancelled SDK cannot receive tools. Cancellation cannot immediately abort
    urllib while upstream headers are pending; the existing socket timeout applies.
    """
    def __init__(self, handler, owner):
        self.handler, self.owner = handler, owner
        self.stopped = threading.Event()
        self.started = False
        self.error = None
        self.thread = threading.Thread(target=self.run, name='chat-heartbeat', daemon=True)

    def start(self):
        # Bound a blocked loopback write so stop/join cannot hang indefinitely.
        self.handler.connection.settimeout(10)
        self.thread.start()

    def check(self):
        if self.error:
            raise self.error
        self.owner.timeout('coding')

    def run(self):
        try:
            while not self.stopped.wait(CHAT_HEARTBEAT_SECONDS):
                self.check()
                if not self.started:
                    self.handler.send_response(200)
                    self.handler.send_header('Content-Type', 'text/event-stream')
                    self.handler.end_headers()
                    self.started = True
                self.handler.wfile.write(b'event: response.in_progress\ndata: {"type":"response.in_progress"}\n\n')
                self.handler.wfile.flush()
        except Exception as exc:
            self.error = exc

    def stop(self):
        self.stopped.set()
        self.thread.join()


class _CodingTiming:
    """Metadata-only observations at the HTTP proxy boundary, not token timing.

    Dispatch is immediately before urllib.open (connection/upload included).
    First output is a complete, recognized SSE event, not an arbitrary socket
    byte, keepalive, reasoning event or a claim about upstream inference start.
    Parsing is bounded and never changes the bytes forwarded to the SDK.
    """
    def __init__(self, owner, route):
        self.owner, self.route = owner, route
        self.request_id = secrets.token_hex(12)
        self.started = time.monotonic()
        self.attempt = 0
        self.attempt_started = None
        self.attempt_closed = True
        self.finished = False
        self.response_format = None
        self.response_bytes = 0
        self.status = None
        self.stream_terminal_event = None
        self.emit('request_received')

    def emit(self, event, **fields):
        now = time.monotonic()
        self.owner.record(phase='coding_timing', event=event,
                          request_id=self.request_id, route=self.route, attempt=self.attempt,
                          request_elapsed_seconds=round(now - self.started, 6),
                          attempt_elapsed_seconds=(round(now - self.attempt_started, 6)
                                                   if self.attempt_started is not None else None),
                          **fields)

    def dispatch(self, attempt):
        self.attempt, self.attempt_started = attempt, time.monotonic()
        self.attempt_closed = False
        self.response_format = self.status = None
        self.response_bytes = 0
        self.stream_terminal_event = None
        self.first_output = False
        self.pending = b''
        self.data_lines = []
        self.frame_bytes = 0
        self.discard_event = False
        self.discard_line = False
        self.skipped_frames = 0
        self.emit('request_dispatch')

    def headers(self, response):
        self.status = response.status
        content_type = response.headers.get('Content-Type', '').split(';', 1)[0].strip().lower()
        self.response_format = ('sse' if content_type == 'text/event-stream' else
                                'json' if content_type == 'application/json' else 'other')
        self.emit('response_headers', status=self.status, response_format=self.response_format)

    @staticmethod
    def _has_output(item):
        if not isinstance(item, dict):
            return False
        if item.get('type') in ('function_call', 'custom_tool_call'):
            return any(isinstance(item.get(key), str) and item[key].strip()
                       for key in ('arguments', 'input'))
        content = item.get('content')
        if not isinstance(content, list):
            return False
        return any(isinstance(part, dict) and part.get('type') in ('output_text', 'refusal')
                   and isinstance(part.get('text', part.get('refusal')), str)
                   and part.get('text', part.get('refusal')).strip()
                   for part in content)

    def _event(self):
        if self.discard_event:
            self.skipped_frames += 1
        elif self.data_lines:
            data = b'\n'.join(self.data_lines)
            if data == b'[DONE]':
                self.stream_terminal_event = self.stream_terminal_event or 'done_marker'
            else:
                try:
                    value = json.loads(data)
                except (ValueError, UnicodeError, RecursionError):
                    value = None
                if isinstance(value, dict):
                    kind = value.get('type')
                    if kind in ('response.completed', 'response.failed', 'response.incomplete', 'error'):
                        self.stream_terminal_event = kind
                    useful = (kind in ('response.output_text.delta', 'response.refusal.delta',
                                       'response.function_call_arguments.delta', 'response.custom_tool_call_input.delta')
                              and isinstance(value.get('delta'), str) and bool(value['delta'].strip()))
                    if kind == 'response.output_item.done':
                        useful = self._has_output(value.get('item'))
                    elif kind == 'response.completed' and isinstance(value.get('response'), dict):
                        outputs = value['response'].get('output')
                        useful = isinstance(outputs, list) and any(self._has_output(item) for item in outputs)
                    if useful and not self.first_output:
                        self.first_output = True
                        # kind is one of the fixed event names above; never log payloads.
                        self.emit('first_output', output_event=kind)
        self.data_lines, self.frame_bytes, self.discard_event = [], 0, False

    def body(self, chunk):
        if not chunk:
            return
        if not self.response_bytes:
            # Buffered HTTP error prefixes may already have been read by an
            # existing subclass. This is an observed chunk, never a wire byte.
            self.emit('first_body_chunk', response_format=self.response_format)
        self.response_bytes += len(chunk)
        if self.response_format != 'sse':
            return
        self.pending += chunk
        while b'\n' in self.pending:
            line, self.pending = self.pending.split(b'\n', 1)
            line = line.removesuffix(b'\r')
            if self.discard_line:
                self.discard_line = False
                continue
            if not line:
                self._event()
            elif not self.discard_event:
                self.frame_bytes += len(line)
                if self.frame_bytes > _TIMING_FRAME_LIMIT:
                    self.discard_event = True
                    self.data_lines = []
                elif line.startswith(b'data:'):
                    self.data_lines.append(line[5:].removeprefix(b' '))
        if len(self.pending) > _TIMING_FRAME_LIMIT:
            self.pending = b''
            self.discard_event = self.discard_line = True
            self.data_lines = []

    def end_attempt(self, outcome, **fields):
        if self.attempt_closed:
            return
        self.attempt_closed = True
        self.emit('attempt_end', outcome=outcome, status=self.status,
                  response_format=self.response_format, response_bytes=self.response_bytes,
                  stream_terminal_event=self.stream_terminal_event,
                  skipped_sse_frames=self.skipped_frames, **fields)

    def finish(self, outcome, **fields):
        if self.finished:
            return
        self.finished = True
        self.end_attempt(outcome, **fields)
        self.emit('request_end', outcome=outcome, status=self.status,
                  response_format=self.response_format, response_bytes=self.response_bytes,
                  stream_terminal_event=self.stream_terminal_event, **fields)


class TransportFailure(RuntimeError):
    def __init__(self, phase: str, reason: str, status: int = 400, *, retry_limit: int | None = None):
        self.status = status
        self.retry_limit = retry_limit
        super().__init__(f'{phase}: {reason}')


class VisualServiceFailure(TransportFailure):
    """A per-image service failure; preserve written requirements and continue."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')


def endpoint(value: str, name: str) -> str:
    try:
        return secure_url(value)
    except ValueError as exc:
        raise TransportFailure(name, str(exc)) from None



class VisualTransport:
    def __init__(self, evidence: Path, deadline: float, *, reference_contexts=None,
                 coding_idle_seconds=_DEFAULT_IDLE, coding_protocol=None, coding_chat_stream=None):
        self.evidence, self.deadline = evidence, deadline
        if coding_idle_seconds is _DEFAULT_IDLE:
            from core.runtime_config import load_policy, transport_limits
            coding_idle_seconds = transport_limits(load_policy())['coding_idle_seconds']
        self.coding_idle_seconds = coding_idle_seconds
        policy = json.loads((Path(__file__).resolve().parents[2] / 'competition/coding-transport.json').read_text())
        protocols = {'responses', 'chat_completions'}
        if (set(policy) != {'default', 'model_protocols', 'chat_recovery_attempts', 'chat_stream'} or policy['default'] not in protocols
                or type(policy['chat_stream']) is not bool
                or type(policy['chat_recovery_attempts']) is not int or policy['chat_recovery_attempts'] < 0
                or not isinstance(policy['model_protocols'], dict)
                or any(value not in protocols for value in policy['model_protocols'].values())):
            raise TransportFailure('coding', 'invalid coding transport configuration')
        if coding_protocol is not None and coding_protocol not in protocols:
            raise TransportFailure('coding', 'unsupported coding protocol')
        self.protocol_policy, self.coding_protocol = policy, coding_protocol
        self.chat_stream = policy['chat_stream'] if coding_chat_stream is None else coding_chat_stream
        if type(self.chat_stream) is not bool:
            raise TransportFailure('coding', 'invalid Chat stream setting')
        from agents.model.chat_adapter import ChatAdapter
        self.chat_adapter = ChatAdapter()
        self.parse_images = images_enabled()
        self.reference_contexts = reference_contexts or {}
        self.supplement = []
        self.coding_base = endpoint(os.environ['OPENAI_BASE_URL'].strip(), 'coding')
        self.coding_key = os.environ['OPENAI_API_KEY'].strip()
        self.visual_env = {k: os.environ.get(k, '').strip()
                           for k in ('VISUAL_BASE_URL', 'VISUAL_API_KEY', 'VISUAL_MODEL')}
        def selected(*names):
            for name in names:
                value = os.environ.get(name, '').strip()
                if value:
                    return value, name
            return '', 'unset'
        model, model_source = selected('VISUAL_MODEL', 'MODEL')
        base, base_source = selected('VISUAL_BASE_URL', 'OPENAI_BASE_URL', 'BASE_URL')
        key, key_source = selected('VISUAL_API_KEY', 'OPENAI_API_KEY')
        self.models = (model.removeprefix('openai:').strip(),)
        self.visual_base = endpoint(base, 'visual')
        self.visual_key = key
        self.visual_sources = {'model_source': model_source,
                               'base_url_source': base_source, 'api_key_source': key_source}
        self.visual_timeout_setting = os.environ.get('HAIKNOW_VISUAL_TIMEOUT_SECONDS', '').strip()
        self.local_key = secrets.token_urlsafe(32)
        self.failure: TransportFailure | None = None
        self.cache: dict[str, str] = {}
        self.cache_metadata: dict[str, dict] = {}
        self.failed_images: dict[str, str] = {}
        self.visual_requests = self.cache_hits = self.failed_cache_hits = 0
        self.event_index = 0
        self.lock = threading.Lock()
        self.record_lock = threading.Lock()
        self.verification_guard = None
        self.provider_requests = 0
        self.opener = build_opener(NoRedirect())

    def redact(self, text: str) -> str:
        for secret in (self.coding_key, self.visual_key, self.visual_env['VISUAL_API_KEY'], self.local_key):
            if secret:
                text = text.replace(secret, '[REDACTED]')
        return text

    def record(self, **record):
        if str(record.get('phase', '')).startswith('visual'):
            record.update(model_candidates=list(self.models), prompt_version=VISUAL_ANALYSIS_PROMPT_VERSION,
                          **self.visual_sources)
        with self.record_lock:
            self.event_index += 1
            record.update(timestamp=datetime.now(timezone.utc).isoformat(), event_index=self.event_index)
            with (self.evidence / 'transport.jsonl').open('a', encoding='utf-8') as file:
                file.write(self.redact(json.dumps(record, ensure_ascii=False)) + '\n')

    def summary(self):
        return {'parse_reference_images': self.parse_images, 'model_candidates': list(self.models),
                'selected_models': {model: sum(meta['requested_model'] == model for meta in self.cache_metadata.values())
                                    for model in self.models},
                'analyzed_images': len(self.cache),
                'unanalyzed_images': len(self.failed_images), 'requests': self.visual_requests,
                'cache_hits': self.cache_hits, 'failed_cache_hits': self.failed_cache_hits}

    def timeout(self, phase: str, cap: float | None = None) -> float | None:
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise TransportFailure(phase, 'total run time budget exhausted')
        if phase == 'coding' and self.coding_idle_seconds is not None:
            cap = self.coding_idle_seconds if cap is None else min(cap, self.coding_idle_seconds)
        # urllib requires None for an unlimited socket timeout; infinity overflows.
        return min(left, cap) if cap is not None else (None if math.isinf(left) else left)

    def coding_response(self, request: Request, input_bytes: int, forwarded_bytes: int,
                        retry_proxy_reset: bool):
        """Retry only a bounded gateway reset envelope, before delivering SDK output."""
        timing = getattr(request, '_haiknow_timing', None)
        correlation = {'request_id': timing.request_id} if timing else {}
        for attempt in range(len(PROXY_RETRY_DELAYS) + 1):
            timeout = self.timeout('coding')
            with self.lock:
                self.provider_requests += 1
            if timing:
                timing.dispatch(attempt + 1)
            try:
                response = self.opener.open(request, timeout=timeout)
            except HTTPError as exc:
                if timing:
                    timing.headers(exc)
                if exc.code == 413 or 300 <= exc.code < 400:
                    exc.close()
                    raise TransportFailure('coding', f'HTTP {exc.code}; input_bytes={input_bytes}; forwarded_bytes={forwarded_bytes}; no retry',
                                           413 if exc.code == 413 else 400) from None
                response = exc  # Keep the SDK's existing handling of other statuses.
            else:
                if timing:
                    timing.headers(response)
            try:
                self.record(phase='coding', status=response.status, input_bytes=input_bytes,
                            forwarded_bytes=forwarded_bytes, attempt=attempt + 1, **correlation)
                prefix = b''
                if retry_proxy_reset and response.status == 400:
                    prefix = response.read(PROXY_ERROR_MAX_BYTES + 1)
                    error = None
                    if len(prefix) <= PROXY_ERROR_MAX_BYTES:
                        try:
                            envelope = json.loads(prefix)
                            error = envelope.get('error') if isinstance(envelope, dict) else None
                        except (ValueError, UnicodeError):
                            pass
                    reason = None
                    if (isinstance(error, dict) and error.get('code') == 'proxy_error'
                            and isinstance(error.get('message'), str)):
                        reason = next((value for fragment, value in PROXY_RESET_REASONS.items()
                                       if fragment in error['message'].lower()), None)
                    if reason and attempt < len(PROXY_RETRY_DELAYS):
                        if timing:
                            timing.body(prefix)
                            timing.end_attempt('proxy_retry')
                        response.close()
                        delay = self.timeout('coding', cap=PROXY_RETRY_DELAYS[attempt])
                        self.record(phase='coding_retry', next_attempt=attempt + 2,
                                    max_attempts=len(PROXY_RETRY_DELAYS) + 1, delay_seconds=delay,
                                    attempt=attempt + 1, **correlation,
                                    reason=reason)
                        time.sleep(delay)
                        self.timeout('coding')
                        continue  # Reuse exact request bytes; no SDK turn/tool replay.
                return response, prefix
            except BaseException:
                response.close()
                raise

    def analyze(self, item: dict) -> str:
        url = item.get('image_url')
        if not isinstance(url, str) or not url.startswith('data:image/'):
            raise TransportFailure('visual', 'only inline SDK image data is supported; no remote URL fetch')
        try:
            header, data = url.split(',', 1)
            if header not in ('data:image/png;base64', 'data:image/jpeg;base64',
                              'data:image/webp;base64', 'data:image/gif;base64'):
                raise ValueError()
            image_bytes = base64.b64decode(data, validate=True)
            if not image_bytes:
                raise ValueError()
        except ValueError:
            raise TransportFailure('visual', 'invalid inline image encoding') from None
        digest = sha256(image_bytes).hexdigest()
        self.timeout('visual')  # A cached result must not bypass the run deadline.
        if not self.parse_images:
            self.record(phase='visual_skipped', image_sha256=digest, reason='disabled')
            return 'UNANALYZED: reference image parsing is disabled. Use the written requirements; do not OCR or infer image text.'
        if digest not in self.reference_contexts:
            self.record(phase='visual_skipped', image_sha256=digest, reason='no_explicit_functional_delegation')
            return ('UNANALYZED: no original requirement explicitly delegates functional text to this image. '
                    'Style-only, unrelated, or unmapped images are not OCR sources. Use the written requirements.')
        if digest in self.cache:
            self.cache_hits += 1
            self.record(phase='visual', image_sha256=digest, cache_hit=True, **self.cache_metadata[digest])
            return self.cache[digest]
        if digest in self.failed_images:
            self.failed_cache_hits += 1
            self.record(phase='visual_unanalyzed', image_sha256=digest, cache_hit=True)
            return self.failed_images[digest]
        try:
            return self._request_analysis(url, image_bytes, digest)
        except VisualServiceFailure as exc:
            self.timeout('visual')  # Budget exhaustion is terminal, never a visual downgrade.
            reason = self.redact(str(exc))[:1000]
            note = (f'WARNING: UNANALYZED reference SHA256 {digest}: {reason}. '
                    'Continue with written requirements. Do not claim to have inspected this image. '
                    'Preserve the original asset; do not retry or bypass the visual service.')
            self.failed_images[digest] = note
            (self.evidence / 'visual-failures.json').write_text(
                json.dumps(self.failed_images, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
            self.record(phase='visual_unanalyzed', image_sha256=digest, cache_hit=False, error=reason)
            print(f'[haiknow] WARNING UNANALYZED: image_sha256={digest}; {reason}', flush=True)
            return note

    def _request_analysis(self, url: str, image_bytes: bytes, digest: str) -> str:
        base = endpoint(self.visual_base, 'visual')
        visual_timeout = VISUAL_TIMEOUT
        if self.visual_timeout_setting:
            try:
                visual_timeout = float(self.visual_timeout_setting)
                if not 1 <= visual_timeout <= 600:
                    raise ValueError()
            except ValueError:
                raise TransportFailure('visual', 'HAIKNOW_VISUAL_TIMEOUT_SECONDS must be 1..600') from None
        started = time.monotonic()
        failures = []
        for attempt, model in enumerate(self.models, 1):
            wait_seconds = self.timeout('visual', visual_timeout)
            body = encode({'model': model, 'messages': [
                {'role': 'system', 'content': build_visual_analysis_prompt()},
                {'role': 'user', 'content': [{'type': 'text', 'text': 'Original requirement context and allowed extraction targets:\n' +
                                            json.dumps(self.reference_contexts[digest], ensure_ascii=False)},
                                            {'type': 'image_url', 'image_url': {'url': url}}]}]})
            request = Request(base + '/chat/completions', body,
                              {'Authorization': 'Bearer ' + self.visual_key,
                               'Content-Type': 'application/json', 'Accept-Encoding': 'identity'})
            attempt_started = time.monotonic()
            self.visual_requests += 1
            self.record(phase='visual_request', image_sha256=digest, image_bytes=len(image_bytes),
                        requested_model=model, candidate_index=attempt, attempt=attempt,
                        request_bytes=len(body), timeout_seconds=wait_seconds,
                        endpoint=base + '/chat/completions', configured_visual_model=self.visual_env['VISUAL_MODEL'],
                        mime_type=url.split(';', 1)[0].removeprefix('data:'))
            print(f'[haiknow] visual request: image_sha256={digest}; request_bytes={len(body)}; '
                  f'model={model}; candidate={attempt}/{len(self.models)}; '
                  f'io_timeout_seconds={wait_seconds:.1f}', flush=True)
            request_id = ''
            http_status = None
            details = {}
            result = {}
            try:
                with self.opener.open(request, timeout=wait_seconds) as response:
                    http_status = response.status
                    request_id = self.redact(response.headers.get('x-onr-request-id') or
                                             response.headers.get('x-request-id', ''))[:200]
                    raw = response.read(1024 * 1024 + 1)
                    if len(raw) > 1024 * 1024:
                        raise ValueError('analysis response exceeds 1 MiB')
                    result = json.loads(raw)
                choice = result['choices'][0]
                note = choice['message']['content']
                if not isinstance(note, str):
                    raise ValueError()
                # MiniMax may put its reasoning before the final text in content.
                note = note.strip()
                while note.startswith('<think>'):
                    _, closing, note = note.partition('</think>')
                    if not closing:
                        raise ValueError()
                    note = note.strip()
                if (not note or choice.get('finish_reason') in ('length', 'content_filter')
                        or choice['message'].get('refusal')):
                    raise ValueError()
                facts = parse_visual_facts(note, self.reference_contexts[digest])
                self.timeout('visual')
            except HTTPError as exc:
                http_status = exc.code
                request_id = self.redact(exc.headers.get('x-onr-request-id') or
                                         exc.headers.get('x-request-id', ''))[:200]
                try:
                    raw = exc.read(4097)
                    error = json.loads(raw).get('error') if len(raw) <= 4096 else None
                    if isinstance(error, str):
                        error = {'message': error}
                    if isinstance(error, dict):
                        details = {'error_' + key: self.redact(str(error[key]))[:512]
                                   for key in ('type', 'code', 'message') if key in error}
                except (ValueError, AttributeError, OSError):
                    pass  # Diagnostic parsing cannot replace the original HTTP failure.
                finally:
                    exc.close()
                reason = f'HTTP {http_status}'
                details.setdefault('error_type', 'http_error')
            except (OSError, HTTPException) as exc:
                reason = ('timeout' if isinstance(exc, TimeoutError) or
                          isinstance(getattr(exc, 'reason', None), TimeoutError) else 'connection failure')
                details = {'error_type': reason}
            except (ValueError, KeyError, IndexError, TypeError, AttributeError):
                reason = 'response has no valid analysis text'
                details = {'error_type': 'invalid_response'}
                if isinstance(result, dict):
                    details.update(response_model=result.get('model'), usage=result.get('usage'))
            else:
                # Only validated scoped facts become model context or searchable requirements.
                facts = json.loads(self.redact(json.dumps(facts, ensure_ascii=False)))
                for fact in facts:
                    self.supplement.append({**fact, 'image_sha256': digest, 'model': model,
                                            'prompt_version': VISUAL_ANALYSIS_PROMPT_VERSION})
                note = json.dumps({'facts': facts, 'unresolved': 'Unreadable or non-required text is not included.'}, ensure_ascii=False)
                self.cache[digest] = (f'[Visual analysis of image SHA256 {digest}; model={model}; '
                                      f'task text remains authoritative]\n{note}')
                self.cache_metadata[digest] = {
                    'requested_model': model, 'candidate_index': attempt,
                    'response_model': self.redact(str(result.get('model', '')))[:200] or None,
                    'response_id': self.redact(str(result.get('id', '')))[:200] or None,
                    'request_id': request_id,
                    'context_sha256': sha256(encode(self.reference_contexts[digest])).hexdigest(),
                }
                (self.evidence / 'visual-cache.json').write_text(
                    json.dumps(self.cache, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
                self.record(phase='visual', image_sha256=digest, cache_hit=False,
                            image_bytes=len(image_bytes), request_bytes=len(body), analysis_chars=len(note),
                            attempts=attempt, elapsed_seconds=round(time.monotonic() - started, 3),
                            http_status=http_status, usage=result.get('usage'),
                            finish_reason=choice.get('finish_reason'), **self.cache_metadata[digest])
                print(self.redact(f'[haiknow] visual success: image_sha256={digest}; requested_model={model}; '
                      f'response_model={self.cache_metadata[digest]["response_model"]}; attempts={attempt}; '
                      f'elapsed_seconds={time.monotonic() - started:.3f}; request_id={request_id}; '
                      f'usage={json.dumps(result.get("usage"))}'), flush=True)
                return self.cache[digest]  # A successful candidate ends this image's chain immediately.

            self.record(phase='visual_error', image_sha256=digest, attempt=attempt, candidate_index=attempt,
                        requested_model=model, http_status=http_status, request_id=request_id,
                        elapsed_seconds=round(time.monotonic() - attempt_started, 3), **details)
            print(self.redact(f'[haiknow] visual error: model={model}; {reason}; candidate={attempt}; '
                             f'request_id={request_id}; {json.dumps(details)}'), flush=True)
            self.timeout('visual')  # No fallback or downgrade after the overall deadline.
            failures.append(f'{model}: {reason}')
            if attempt < len(self.models):
                next_model = self.models[attempt]
                self.record(phase='visual_fallback', image_sha256=digest, requested_model=model,
                            candidate_index=attempt, next_model=next_model, reason=reason)
                print(f'[haiknow] visual fallback: {model} -> {next_model}; {reason}', flush=True)
        raise VisualServiceFailure('visual', f'all candidates failed; attempts={len(failures)}; '
                                   + '; '.join(failures))

    def rewrite(self, value):
        if isinstance(value, dict):
            if value.get('type') == 'input_image':
                return {'type': 'input_text', 'text': self.analyze(value)}
            return {k: self.rewrite(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.rewrite(v) for v in value]
        return value

    def restrict_tools(self, payload):
        if 'tools' not in payload:
            return
        allowed, removed = [], []
        for tool in payload['tools']:
            name = tool.get('name', tool.get('type', 'unknown'))
            if tool.get('type') in ('function', 'custom') and name in CODING_TOOLS:
                allowed.append(tool)
            else:
                removed.append(name)
        payload['tools'] = allowed
        self.record(phase='coding_tools', allowed=[t['name'] for t in allowed], removed=removed)

    def __enter__(self):
        config = {
            'phase': 'visual_config', 'endpoint': self.visual_base + '/chat/completions',
            'model_candidates': list(self.models), 'attempts_per_model': 1,
            'selection_policy': 'submitted_visual_model', **self.visual_sources,
        }
        self.record(**config)
        print('[haiknow] visual config: ' + self.redact(json.dumps(config, ensure_ascii=False)), flush=True)
        owner = self

        class Handler(BaseHTTPRequestHandler):
            # HTTP/1.0 close-delimited responses preserve upstream SSE framing.
            def log_message(self, *args):
                pass

            def error(self, status, message):
                data = encode({'error': {'message': message, 'type': 'haiknow_transport'}})
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                if self.headers.get('Authorization') != 'Bearer ' + owner.local_key:
                    self.error(401, 'invalid local transport authorization')
                    return
                if self.path not in ('/v1/responses', '/v1/responses/compact'):
                    self.error(404, 'unsupported coding route')
                    return
                started = False
                timing = _CodingTiming(owner, self.path)
                downstream_write = False
                from agents.model.chat_adapter import ChatProtocolError
                chat_stage = None
                chat_stats = {'received_bytes': 0, 'done_received': False, 'finish_reason': None,
                              'usage_received': False, 'tool_count': 0}
                heartbeat = None
                request_offered = None
                def deliver_chat(reply):
                    nonlocal chat_stage, started, downstream_write
                    from agents.model.chat_adapter import stream_events
                    chat_stage = 'validate_response'
                    complete = owner.chat_adapter.complete(reply, stats=chat_stats, offered=request_offered)
                    owner.record(phase='coding_chat', protocol=protocol, model=payload['model'],
                                 upstream_stream=owner.chat_stream,
                                 response_model=complete.get('model'), usage=complete['usage'],
                                 tool_name_aliases=chat_stats.get('tool_name_aliases', 0),
                                 request_id=timing.request_id)
                    downstream_write = True
                    if not started:
                        self.send_response(200)
                        self.send_header('Content-Type', 'text/event-stream')
                        self.end_headers()
                        started = True
                    for event in stream_events(complete):
                        chunk = b'event: ' + event['type'].encode() + b'\ndata: ' + encode(event) + b'\n\n'
                        if owner.chat_stream:
                            timing.body(chunk)
                        self.wfile.write(chunk)
                        self.wfile.flush()
                    downstream_write = False
                    timing.finish('chat_completed')
                try:
                    with owner.lock:
                        if owner.failure:
                            raise owner.failure
                        length = int(self.headers.get('Content-Length', '0'))
                        if self.headers.get('Content-Encoding', 'identity') != 'identity':
                            raise TransportFailure('coding', 'unsupported compressed request')
                        if not 0 < length <= MAX_REQUEST_BYTES:
                            raise TransportFailure('coding', 'local request exceeds 32 MiB or lacks Content-Length', 413)
                        payload = json.loads(self.rfile.read(length))
                        if not isinstance(payload, dict):
                            raise TransportFailure('coding', 'expected JSON object')
                        # Only actual conversation input, never tool definitions/JSON schemas.
                        if 'input' in payload:
                            payload['input'] = owner.rewrite(payload['input'])
                        owner.restrict_tools(payload)
                        if owner.verification_guard and self.path == '/v1/responses':
                            before = len(owner.verification_guard.events)
                            owner.verification_guard.apply(payload)
                            if len(owner.verification_guard.events) > before:
                                owner.record(phase='verification_guard', **owner.verification_guard.events[-1])
                        protocol = owner.coding_protocol or owner.protocol_policy['model_protocols'].get(
                            payload.get('model'), owner.protocol_policy['default'])
                        if protocol == 'chat_completions':
                            if self.path != '/v1/responses':
                                raise TransportFailure('coding', 'Chat requires SDK text compaction; remote compact is unsupported')
                            chat_stage = 'prepare'
                            payload = owner.chat_adapter.prepare(payload, stream=owner.chat_stream)
                            request_offered = dict(owner.chat_adapter.offered)
                        body = encode(payload)
                    ignored = {'host', 'authorization', 'content-length', 'connection', 'transfer-encoding',
                               'accept-encoding', 'proxy-authorization', 'cookie', 'upgrade'}
                    headers = {k: v for k, v in self.headers.items() if k.lower() not in ignored}
                    headers.update({'Authorization': 'Bearer ' + owner.coding_key,
                                    'Content-Type': 'application/json', 'Accept-Encoding': 'identity'})
                    upstream_path = '/chat/completions' if protocol == 'chat_completions' else self.path[len('/v1'):]
                    request = Request(owner.coding_base + upstream_path, body, headers)
                    request._haiknow_timing = timing
                    if protocol == 'chat_completions' and not owner.chat_stream:
                        from agents.model.chat_adapter import read_chat_json
                        chat_stage = 'read_json'
                        heartbeat = _ChatHeartbeat(self, owner)
                        heartbeat.start()
                        try:
                            response, _ = owner.coding_response(request, length, len(body), retry_proxy_reset=True)
                            with response:
                                if response.status != 200:
                                    raise TransportFailure('coding', f'HTTP {response.status}', response.status)
                                if response.headers.get('Content-Type', '').split(';', 1)[0].strip().lower() != 'application/json':
                                    raise TransportFailure('coding', 'expected non-streaming Chat JSON response')
                                reply = read_chat_json(response, heartbeat.check, stats=chat_stats, observe=timing.body)
                        finally:
                            heartbeat.stop()
                            started = heartbeat.started
                        heartbeat.check()
                        deliver_chat(reply)
                        return
                    response, prefix = owner.coding_response(
                        request, length, len(body), retry_proxy_reset=self.path == '/v1/responses')
                    with response:
                        if protocol == 'chat_completions' and response.status == 200:
                            from agents.model.chat_adapter import read_chat_stream
                            if 'text/event-stream' not in response.headers.get('Content-Type', ''):
                                raise TransportFailure('coding', 'expected streaming Chat response')
                            # Upstream emits deltas during reasoning. Keep the SDK alive
                            # with progress events, never partial executable tool items.
                            self.send_response(200)
                            self.send_header('Content-Type', 'text/event-stream')
                            self.end_headers()
                            started = True
                            last_progress = [0.0]
                            def progress():
                                nonlocal downstream_write
                                owner.timeout('coding')
                                now = time.monotonic()
                                if now - last_progress[0] >= 5:
                                    downstream_write = True
                                    self.wfile.write(b'event: response.in_progress\ndata: {"type":"response.in_progress"}\n\n')
                                    self.wfile.flush()
                                    downstream_write = False
                                    last_progress[0] = now
                            chat_stage = 'read_stream'
                            reply = read_chat_stream(response, progress, stats=chat_stats)
                            deliver_chat(reply)
                            return
                        downstream_write = True
                        self.send_response(response.status)
                        for key, value in response.headers.items():
                            if key.lower() not in ('transfer-encoding', 'connection', 'content-length'):
                                self.send_header(key, value)
                        self.end_headers()
                        started = True
                        if prefix:
                            timing.body(prefix)
                            self.wfile.write(prefix)
                            self.wfile.flush()
                        downstream_write = False
                        while chunk := response.read1(65536):
                            owner.timeout('coding')
                            timing.body(chunk)
                            downstream_write = True
                            self.wfile.write(chunk)
                            self.wfile.flush()
                            downstream_write = False
                        remaining = getattr(response, 'length', None)
                        if isinstance(remaining, int) and remaining > 0:
                            timing.finish('failure', error_type='incomplete_body')
                        else:
                            timing.finish('http_error' if response.status >= 400 else 'transport_eof')
                except (BrokenPipeError, ConnectionResetError) as exc:
                    timing.finish('client_cancelled' if downstream_write or (heartbeat and heartbeat.error) else 'failure',
                                  error_type=type(exc).__name__)
                    pass  # SDK cancellation; controller owns run status.
                except Exception as exc:
                    timing.finish('failure', error_type=type(exc).__name__)
                    if isinstance(exc, TransportFailure):
                        failure = exc
                    elif isinstance(exc, ChatProtocolError):
                        diagnostic = dict(phase='coding_chat_error', protocol='chat_completions', upstream_stream=owner.chat_stream,
                                          stage=chat_stage, reason=exc.code, detail=exc.detail, request_id=timing.request_id,
                                          **chat_stats)
                        owner.record(**diagnostic)
                        # Platform project.zip omits .arc; keep metadata visible
                        # in stdout as well, without model text or tool arguments.
                        print('[haiknow] ' + owner.redact(json.dumps(diagnostic)), flush=True)
                        # Only a rejected upstream response may resume. Nothing
                        # from that response has reached the SDK tool executor.
                        # Auth, refusal, tool-policy and history failures stay fatal.
                        recoverable = (chat_stage == 'read_json' and exc.code == 'invalid_json_response') or (chat_stage == 'read_stream' and (
                            exc.code in {'missing_done', 'missing_terminal_choice'} or
                            (exc.code == 'invalid_sse_frame' and exc.detail == 'invalid_json'))) or (
                            chat_stage == 'validate_response' and (
                            (exc.code == 'unfinished_turn' and chat_stats['finish_reason'] == 'length') or
                            (exc.code == 'invalid_tool_call' and exc.detail in {
                                'missing_tool_call_id', 'missing_tool_name', 'unoffered_tool_name'})))
                        recoverable = (recoverable and not chat_stats.get('refusal_seen')
                                       and chat_stats['finish_reason'] != 'content_filter')
                        if recoverable and exc.detail == 'unoffered_tool_name':
                            owner.chat_adapter.note_tool_mismatch()
                        failure = TransportFailure('coding', 'transport failure (ChatProtocolError: ' + exc.code + ')',
                            # Keep 400 on loopback so the SDK returns control;
                            # a 5xx here causes a second, hidden SDK retry loop.
                            retry_limit=owner.protocol_policy['chat_recovery_attempts'] if recoverable else None)
                    elif (isinstance(exc, (TimeoutError, URLError)) or
                          (isinstance(exc, IncompleteRead) and timing.status == 200)):
                        # Resume through the existing controller/thread recovery;
                        # never replay a response after tools may have executed.
                        failure = TransportFailure('coding', 'timeout or connection failure')
                    else:
                        failure = TransportFailure('coding', 'transport failure (' + type(exc).__name__ + ')')
                    owner.failure = failure
                    owner.record(phase='failure', error=str(failure))
                    print('[haiknow] ' + str(failure), flush=True)
                    if not started:
                        try:
                            self.error(failure.status, str(failure))
                        except (BrokenPipeError, ConnectionResetError):
                            pass  # Upstream failure was already recorded before SDK cancellation.

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={'poll_interval': .05}, daemon=True)
        self.thread.start()
        self.base_url = f'http://127.0.0.1:{self.server.server_port}/v1'
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)

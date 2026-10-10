"""Bounded read-only SDK observations captured before SDK-owned cleanup.

Private SDK observation points live in this adapter and are integration-tested
against both supported SDKs. No commands, limits, approvals or RPC data change.
"""
from collections import deque
from datetime import datetime, timezone
import importlib.metadata
import json
import os
from pathlib import Path
import signal
import subprocess
import threading
import time

from arcbench_security import child_env

MAX_LOG_BYTES = 1048576
SAMPLE_SECONDS = 5
CGROUP_ROOT = Path('/sys/fs/cgroup')


def timestamp():
    return datetime.now(timezone.utc).isoformat()


class Journal:
    """At most two segments; truncation happens only after redaction."""
    def __init__(self, path):
        self.path = path
        self.previous = path.with_name(path.stem + '.previous' + path.suffix)
        self.limit = MAX_LOG_BYTES
        self.rotations = 0
        self.truncated_records = 0
        self.lock = threading.Lock()

    def write(self, text):
        data = (text.rstrip('\n') + '\n').encode('utf-8')
        with self.lock:
            if len(data) > self.limit:
                marker = b'[record truncated; recent tail follows]\n'[:self.limit]
                available = self.limit - len(marker)
                # Reserve marker bytes and drop an incomplete leading codepoint.
                tail = data[-available:].decode('utf-8', errors='ignore').encode('utf-8') if available else b''
                data = marker + tail
                self.truncated_records += 1
            size = self.path.stat().st_size if self.path.exists() else 0
            if size + len(data) > self.limit:
                self.path.replace(self.previous)
                self.rotations += 1
            with self.path.open('ab') as stream:
                stream.write(data)

    def summary(self):
        return {'max_bytes_per_segment': self.limit, 'max_segments': 2,
                'rotations': self.rotations, 'truncated_records': self.truncated_records}


class ObservedStderr(deque):
    def __init__(self, previous, observe):
        super().__init__(previous, maxlen=previous.maxlen)
        self.observe = observe

    def append(self, line):
        super().append(line)  # Preserve the SDK's exact retained buffer semantics.
        self.observe(line)


def resource_snapshot(pid):
    result = {'timestamp': timestamp(), 'cgroup': {}, 'process': {'pid': pid}}
    for name in ('memory.current', 'memory.peak', 'memory.max', 'memory.events'):
        try:
            text = (CGROUP_ROOT / name).read_text()[:65536].strip()
            result['cgroup'][name] = (dict((k, int(v)) for k, v in (line.split() for line in text.splitlines()))
                                      if name == 'memory.events' else int(text) if text.isdigit() else text)
        except (OSError, ValueError) as exc:
            result['cgroup'][name] = {'unavailable': type(exc).__name__}
    if pid is not None:
        try:
            status = Path(f'/proc/{pid}/status')
            if status.exists():
                wanted = {'Name', 'Pid', 'PPid', 'VmRSS', 'VmHWM', 'Threads'}
                result['process'].update({key: value.strip() for key, value in
                    (line.split(':', 1) for line in status.read_text().splitlines() if ':' in line) if key in wanted})
            elif os.uname().sysname == 'Darwin':
                # Only this child's numeric process information; never argv/env.
                checked = subprocess.run(['ps', '-o', 'pid=,ppid=,rss=', '-p', str(pid)],
                    capture_output=True, text=True, timeout=.2, env=child_env())
                fields = checked.stdout.split()
                if checked.returncode == 0 and len(fields) == 3:
                    result['process'].update(zip(('pid', 'ppid', 'rss_kib'), map(int, fields)))
                else:
                    result['process']['unavailable'] = 'process not visible'
            else:
                result['process']['unavailable'] = 'proc status not available'
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            result['process']['unavailable'] = type(exc).__name__
    return result


class SDKDiagnostics:
    def __init__(self, codex, evidence: Path, redact, deadline: float):
        self.client = getattr(codex, '_client', None)
        self.process = getattr(self.client, '_proc', None)
        self.evidence = evidence
        self.redact = redact
        self.deadline = deadline
        self.errors = []
        self.active = False
        self.stop = threading.Event()
        self.worker = None
        self.original_notification = None
        self.notification_had_override = '_coerce_notification' in getattr(self.client, '__dict__', {})
        self.initial_resources = {}
        self.journals = {name: Journal(evidence / name) for name in
                         ('sdk-stderr.log', 'sdk-events.jsonl', 'sdk-resources.jsonl')}

    def _safe(self, action):
        try:
            return action()
        except TimeoutError:
            raise  # Never swallow the controller's total-budget signal.
        except Exception as exc:
            self.errors.append(self.redact(f'{type(exc).__name__}: {exc}')[:1000])
            self.errors = self.errors[-20:]
            return None

    def _record(self, name, value):
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        self.journals[name].write(self.redact(text))

    def _stderr(self, line):
        if self.active:
            self._safe(lambda: self._record('sdk-stderr.log', line))

    def _notification(self, method, params):
        if not self.active or not isinstance(params, dict):
            return
        if method not in ('item/started', 'item/completed'):
            return
        item = params.get('item', {})
        if not isinstance(item, dict) or item.get('type') not in ('commandExecution', 'fileChange', 'mcpToolCall'):
            return  # No reasoning, model messages, full output, images or auth RPC.
        selected = {key: item[key] for key in ('id', 'type', 'status', 'command', 'cwd',
                    'processId', 'exitCode', 'durationMs', 'server', 'tool') if key in item}
        # Redact before truncation, without changing the SDK's actual message.
        for key, value in selected.items():
            if isinstance(value, str):
                selected[key] = self.redact(value)[:8192]
        if item.get('type') == 'fileChange':
            selected['paths'] = [self.redact(str(change.get('path', '')))[:1000]
                                 for change in item.get('changes', [])[:100] if isinstance(change, dict)]
        self._record('sdk-events.jsonl', {'timestamp': timestamp(), 'method': method,
                     'thread_id': params.get('threadId'), 'turn_id': params.get('turnId'), 'item': selected})

    def _sample(self):
        sample = resource_snapshot(self.process.pid if self.process is not None else None)
        self._record('sdk-resources.jsonl', sample)
        return sample

    def __enter__(self):
        self.active = True
        def install():
            previous = getattr(self.client, '_stderr_lines', None)
            if isinstance(previous, deque):
                existing = list(previous)
                self.client._stderr_lines = ObservedStderr(previous, self._stderr)
                for line in existing:
                    self._stderr(line)
            else:
                self.errors.append('SDK retained stderr buffer unavailable')
            original = getattr(self.client, '_coerce_notification', None)
            if callable(original):
                self.original_notification = original
                def observe(method, params):
                    self._safe(lambda: self._notification(method, params))
                    return original(method, params)
                self.client._coerce_notification = observe
            else:
                self.errors.append('SDK notification observation point unavailable')
        self._safe(install)
        self.initial_resources = self._safe(self._sample) or {}
        def sample_loop():
            while not self.stop.wait(SAMPLE_SECONDS):
                if time.monotonic() >= self.deadline:
                    return
                self._safe(self._sample)
        self.worker = threading.Thread(target=sample_loop, daemon=True, name='sdk-resource-observer')
        self.worker.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            self.stop.set()
            if self.worker:
                self.worker.join(timeout=.5)
            process = {'state': 'unavailable', 'pid': None, 'returncode': None}
            if self.process is not None:
                code = self.process.poll()
                if code is None and exc is not None:
                    try:
                        code = self.process.wait(timeout=max(0, min(.2, self.deadline - time.monotonic())))
                    except subprocess.TimeoutExpired:
                        pass
                process = {'pid': self.process.pid, 'returncode': code,
                           'state': 'running' if code is None else 'signaled' if code < 0 else 'exited'}
                if code is not None and code < 0:
                    try:
                        process['signal'] = signal.Signals(-code).name
                    except ValueError:
                        process['signal'] = str(-code)
                drain = getattr(self.client, '_stderr_thread', None)
                if code is not None and drain is not None:
                    drain.join(timeout=max(0, min(.2, self.deadline - time.monotonic())))
            final_resources = self._safe(self._sample) or {}
            before = self.initial_resources.get('cgroup', {}).get('memory.events', {})
            after = final_resources.get('cgroup', {}).get('memory.events', {})
            deltas = {key: value - before[key] for key, value in after.items()
                      if isinstance(value, int) and isinstance(before.get(key), int)}
            snapshot = {'timestamp': timestamp(), 'phase': 'before_sdk_cleanup',
                        'exception_type': exc_type.__name__ if exc_type else None,
                        'process': process, 'diagnostic_errors': self.errors,
                        'sdk_version': importlib.metadata.version('openai-codex'),
                        'resources_initial': self.initial_resources, 'resources_final': final_resources,
                        'resource_deltas': deltas,
                        'journals': {name: log.summary() for name, log in self.journals.items()},
                        'limits': 'First startup stderr is from the SDK retained buffer; later stderr is streamed. Logs rotate. Command text is bounded; no full tool output/reasoning/auth RPC. Cgroup deltas alone do not identify the killed process.'}
            path = self.evidence / 'sdk-diagnostics.json'
            temporary = path.with_suffix('.json.tmp')
            temporary.write_text(self.redact(json.dumps(snapshot, ensure_ascii=False, indent=2)) + '\n')
            temporary.replace(path)
            print('[haiknow] SDK lifecycle: ' + json.dumps({'process': process,
                  'exception_type': snapshot['exception_type'], 'resource_deltas': deltas,
                  'diagnostic_error_count': len(self.errors)}), flush=True)
        except Exception as error:
            if isinstance(error, TimeoutError) and exc is None:
                raise
            print('[haiknow] SDK diagnostics unavailable: ' + self.redact(str(error)), flush=True)
        finally:
            self.active = False
            if self.original_notification is not None:
                if self.notification_had_override:
                    self.client._coerce_notification = self.original_notification
                else:
                    del self.client._coerce_notification
            buffer = getattr(self.client, '_stderr_lines', None)
            if isinstance(buffer, ObservedStderr):
                self.client._stderr_lines = deque(buffer, maxlen=buffer.maxlen)
        return False

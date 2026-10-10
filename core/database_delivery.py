"""Check retained application databases on private copies, never the live files."""
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil


INPUT_PATH = '.arc/haiknow/database-input'
SIDECARS = ('-wal', '-shm', '-journal')
EXCLUDED = {'.git', '.arc', '.haiknow', '.codex', '.agents', 'node_modules',
            'dist', 'build', '__pycache__', 'coverage', 'test-results',
            'playwright-report', 'tests', 'test-e2e', 'test-e2e-retained', '.arc-test-db'}


class DatabaseDeliveryError(RuntimeError):
    pass


def digest(path):
    result = sha256()
    with path.open('rb') as stream:
        for data in iter(lambda: stream.read(1024 * 1024), b''): result.update(data)
    return result.hexdigest()


def safe_path(root, relative):
    """Only regular paths inside the app; do not follow a DB/ancestor symlink."""
    if root.is_symlink():
        raise DatabaseDeliveryError('Database root must not be a symlink')
    relative = Path(relative)
    if relative.is_absolute() or '..' in relative.parts:
        raise DatabaseDeliveryError('Database path must stay inside the application')
    path = root
    for part in relative.parts:
        path = path / part
        if path.is_symlink():
            raise DatabaseDeliveryError('Database path must not contain a symlink: ' + str(relative))
    if path.exists() and not path.is_file():
        raise DatabaseDeliveryError('Expected a database file: ' + str(relative))
    return path


def primary_path(project):
    """Mirror the official backend cwd and template DB environment precedence."""
    value = os.environ.get('ARC_DB_FILE') or os.environ.get('DATABASE_FILE') or 'database.db'
    path = Path(value)
    if not path.is_absolute(): path = project / 'backend' / path
    try:
        relative = path.relative_to(project).as_posix()
    except ValueError as exc:
        raise DatabaseDeliveryError('Application database must be inside the output directory') from exc
    safe_path(project, relative)
    return relative


def is_database_path(name, primary=None):
    base = name
    for suffix in SIDECARS:
        if base.endswith(suffix): base = base[:-len(suffix)]; break
    return base == primary or base.endswith(('.db', '.sqlite', '.sqlite3'))


def inventory(project, primary):
    files = {}
    for directory, dirs, names in os.walk(project, followlinks=False):
        dirs[:] = sorted(n for n in dirs if n not in EXCLUDED and not n.startswith('.env'))
        for name in sorted(names):
            path = Path(directory) / name
            relative = path.relative_to(project).as_posix()
            if is_database_path(relative, primary):
                files[relative] = digest(safe_path(project, relative))
    # An explicitly selected DB may have no suffix or live in an excluded directory.
    for relative in (primary, *(primary + suffix for suffix in SIDECARS)):
        path = safe_path(project, relative)
        if path.exists(): files[relative] = digest(path)
    return dict(sorted(files.items()))


def capture(project, destination):
    """Copy a quiescent DB/WAL/journal group; SQLite never opens the original."""
    project = project.resolve()
    primary = primary_path(project)
    before = inventory(project, primary)
    destination.mkdir(parents=True, exist_ok=False)
    for name in before:
        target = destination / 'files' / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(safe_path(project, name), target)
    if before != inventory(project, primary) or any(
            digest(destination / 'files' / name) != value for name, value in before.items()):
        raise DatabaseDeliveryError('Database files changed during snapshot; stop application writers before validation')
    record = {'schema_version': 1, 'primary': primary, 'files': before}
    (destination / 'manifest.json').write_text(json.dumps(record, sort_keys=True, indent=2) + '\n')
    return record


def capture_input(project):
    destination = project / INPUT_PATH
    capture(project, destination)
    return {'path': INPUT_PATH, 'manifest_sha256': digest(destination / 'manifest.json')}


def input_snapshot(project):
    workspace = project / '.haiknow/workspace.json'
    if not workspace.exists(): return None
    pointer = json.loads(workspace.read_text()).get('database_input')
    if pointer is None: return None  # Older/direct callers still check delivery state.
    if pointer.get('path') != INPUT_PATH:
        raise DatabaseDeliveryError('Invalid incoming database snapshot reference')
    root = project / INPUT_PATH
    manifest = safe_path(project, INPUT_PATH + '/manifest.json')
    if not manifest.is_file() or digest(manifest) != pointer.get('manifest_sha256'):
        raise DatabaseDeliveryError('Incoming database snapshot was removed or changed')
    return root


def load_snapshot(root):
    record = json.loads((root / 'manifest.json').read_text())
    if record.get('schema_version') != 1 or not isinstance(record.get('files'), dict):
        raise DatabaseDeliveryError('Invalid database snapshot manifest')
    safe_path(root / 'files', record['primary'])
    for name, expected in record['files'].items():
        path = safe_path(root / 'files', name)
        if not path.is_file() or digest(path) != expected:
            raise DatabaseDeliveryError('Database snapshot changed: ' + name)
    return record


def frozen_input_files(project):
    root = input_snapshot(project)
    if root is None: return []
    record = load_snapshot(root)
    return [root / 'manifest.json', *(root / 'files' / name for name in record['files'])]


def verify_retained_journeys(project, detached, scratch, evidence, deadline, env, check):
    """Run only app-authored retained-state journeys with controller-owned setup."""
    from core.validation import CheckFailure, read_test_report, run_command, tail, validation_env
    from core.evaluator_isolation import landrun_executable
    import subprocess

    state = check['retained_journeys'] = {'status': 'missing'}
    backend = detached / 'backend'
    tests = backend / 'test-e2e-retained'
    if tests.is_symlink() or not any(
            path.is_file() and not path.is_symlink()
            for extension in ('js', 'cjs', 'mjs', 'ts') for path in tests.glob('*.spec.' + extension)):
        raise DatabaseDeliveryError('Missing backend/test-e2e-retained/*.spec.{js,cjs,mjs,ts}: '
            'write focused browser journeys that exercise pre-existing data after migration; '
            'do not reset, reseed, or replace the retained database in test setup.')
    state['status'] = 'failed'
    executable = backend / 'node_modules/@playwright/test/cli.js'
    if not executable.is_file():
        raise DatabaseDeliveryError('Missing @playwright/test for retained-data journeys')
    runner = scratch / 'retained-runner'
    runner.mkdir(parents=True, exist_ok=True)
    result = runner / 'retained-journeys.json'
    config = runner / 'retained-playwright.config.cjs'
    config.write_text('module.exports = ' + json.dumps({
        'testDir': str(tests), 'testMatch': '*.spec.{js,cjs,mjs,ts}',
        'outputDir': str(runner / 'test-results'),
        'workers': 1, 'retries': 0, 'forbidOnly': True, 'timeout': 30000,
        'use': {'baseURL': env['PLAYWRIGHT_BASE_URL'], 'headless': True},
    }) + ';\n')
    browser_cache = next((path for path in (scratch / 'cache/ms-playwright',
        project / '.arc/haiknow/browser-cache') if path.is_dir()), None)
    child = validation_env(env, runner)
    child['PLAYWRIGHT_JSON_OUTPUT_FILE'] = str(result)
    if browser_cache is not None: child['PLAYWRIGHT_BROWSERS_PATH'] = str(browser_cache)
    # The running application owns DB writes; test fixtures must use its UI/API.
    argv = [str(landrun_executable(detached)), '--rox', '/', '--rwx', str(runner),
            '--rw', '/dev/null', '--unrestricted-network', '--unrestricted-scoped', '--best-effort']
    for key in sorted(child): argv.extend(['--env', key])
    argv.extend(['--', 'node', str(executable), 'test', '--config', str(config), '--reporter=json'])
    result.unlink(missing_ok=True)
    try:
        run_command(argv, backend, evidence / 'retained-journeys.log', deadline,
                    env=child, scratch=runner, report_path=result, browser_cache=browser_cache)
        state['tests'] = read_test_report(result, 'playwright')
        state['status'] = 'passed'
    except (CheckFailure, subprocess.TimeoutExpired) as exc:
        raise DatabaseDeliveryError('Retained-data journeys failed: ' + str(exc) + '\n' + tail(result)) from exc
    finally:
        if result.is_file(): shutil.copyfile(result, evidence / 'retained-journeys.json')


def verify_retained(project, detached, scratch, evidence, deadline, env, *, run_journeys=True):
    """Use already-built dependencies; validate incoming and outgoing state twice."""
    # Import at call time to reuse validator lifecycle helpers without a module cycle.
    from core.validation import stop_process, tail, remaining
    from core.evaluator_isolation import landrun_executable
    from core.time_budget import RunDeadlineExceeded
    from urllib.request import build_opener, ProxyHandler
    from http.client import HTTPException
    import socket
    import subprocess
    import time

    current = scratch / 'database-delivery'
    current_record = capture(project, current)
    roots = [('delivery', current)]
    incoming = input_snapshot(project)
    if incoming is not None: roots.insert(0, ('stage-input', incoming))
    report = {'passed': False, 'checks': [], 'scope': (
        'retained database startup, restart and app-authored stage-input journeys; no official tests'
        if run_journeys else 'retained database startup and restart only; business journeys not run; no official tests')}
    seen = set()
    original_files = current_record['files']
    try:
        for label, root in roots:
            record = load_snapshot(root)
            identity = sha256(json.dumps(record, sort_keys=True).encode()).hexdigest()
            if identity in seen: continue
            seen.add(identity)
            # Keep all database paths together; secondary stores are not substituted for the primary.
            for name in inventory(detached, record['primary']):
                (detached / name).unlink()
            for name in record['files']:
                target = safe_path(detached, name)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(root / 'files' / name, target)
            check = {'source': label, 'database_files': sorted(record['files']),
                     'primary': record['primary'], 'starts': []}
            report['checks'].append(check)
            database = detached / record['primary']
            database.parent.mkdir(parents=True, exist_ok=True)
            with socket.socket() as one, socket.socket() as two:
                one.bind(('127.0.0.1', 0)); two.bind(('127.0.0.1', 0))
                ports = [one.getsockname()[1], two.getsockname()[1]]
            runtime_env = {**env, 'ARC_DB_FILE': str(database), 'DATABASE_FILE': str(database),
                           'PORT': str(ports[0]), 'SPEC_BASE_PORTS': ','.join(map(str, ports)),
                           'ARC_WEB_PORT': str(ports[0]), 'ARCBENCH_WEB_PORT': str(ports[0]),
                           'PLAYWRIGHT_BASE_URL': f'http://127.0.0.1:{ports[1]}',
                           'ARC_WEB_BASE_URL': f'http://127.0.0.1:{ports[1]}'}
            launcher = landrun_executable(detached)
            argv = [str(launcher), '--rox', '/', '--rwx', str(scratch), '--rw', '/dev/null',
                    '--unrestricted-network', '--unrestricted-scoped', '--best-effort']
            for key in sorted(runtime_env): argv.extend(['--env', key])
            argv.extend(['--', 'npm', 'run', 'start'])
            opener = build_opener(ProxyHandler({}))
            for attempt in range(2):
                remaining(deadline)
                logfile = evidence / f'database-{label}-start-{attempt + 1}.log'
                print(f'[verify] retained database {label}: start {attempt + 1}/2', flush=True)
                with logfile.open('w') as log:
                    process = subprocess.Popen(argv, cwd=detached / 'backend', env=runtime_env,
                        stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                    try:
                        ready = False
                        last_probe_error = ''
                        end = time.monotonic() + remaining(deadline, 45)
                        while time.monotonic() < end and process.poll() is None:
                            try:
                                for port in ports:
                                    for endpoint in ('/api/health', '/'):
                                        with opener.open(f'http://127.0.0.1:{port}' + endpoint, timeout=min(2,remaining(deadline))) as response:
                                            body = response.read(1024 * 1024).decode('utf-8', errors='replace')
                                            if response.status != 200 or (endpoint == '/' and
                                                ('<html' not in body.lower() or 'frontend build missing' in body.lower())):
                                                raise OSError('Application is not ready')
                                ready = True; break
                            except RunDeadlineExceeded:
                                raise
                            except (OSError, HTTPException) as exc:
                                last_probe_error = f'{type(exc).__name__}: {exc}'[:1000]
                                time.sleep(.1)
                        remaining(deadline)
                        if not ready or process.poll() is not None:
                            raise DatabaseDeliveryError(f'Retained database {label}, start {attempt + 1} failed. '
                                'Check the application startup log and probe failure. If schema migration fails, '
                                'implement a data-preserving, repeatable migration; do not delete the database.\n'
                                + last_probe_error + '\n' + tail(logfile))
                        check['starts'].append({'attempt': attempt + 1, 'passed': True, 'ports': ports})
                        if label == 'stage-input' and attempt == 1 and record['files']:
                            if record['primary'] in record['files']:
                                if run_journeys:
                                    verify_retained_journeys(project, detached, scratch, evidence, deadline, runtime_env, check)
                                else:
                                    check['retained_journeys'] = {'status': 'not_run_delivery_only'}
                            else:
                                check['retained_journeys'] = {'status': 'not_applicable',
                                    'reason': 'primary_database_absent; secondary stores received startup checks only'}
                    finally:
                        stop_process(process)
        report['passed'] = True
    except BaseException as exc:
        report['error'] = f'{type(exc).__name__}: {exc}'
        raise
    finally:
        changed = inventory(project, current_record['primary']) != original_files
        if changed:
            report.update(passed=False, error='Published database changed during detached validation')
        (evidence / 'database-delivery.json').write_text(json.dumps(report, indent=2) + '\n')
        if changed: raise DatabaseDeliveryError(report['error'])
    return report

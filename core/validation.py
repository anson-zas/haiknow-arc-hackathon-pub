"""Execute local delivery checks. These are internal checks, not official scores."""
from __future__ import annotations

import json
import shutil
from hashlib import sha256
from arcbench_security import child_env
import os
from pathlib import Path
import signal
import socket
import subprocess
import time
import tempfile
from typing import Any
from urllib.error import URLError
from urllib.request import build_opener, ProxyHandler
from core.ui_names import check_names, failure_message, check_semantics, load_name_acceptance, semantic_issue_message
from core.profile import profile_enabled
from core.time_budget import RunDeadlineExceeded
from core.database_delivery import (DatabaseDeliveryError, inventory, is_database_path, primary_path, verify_retained)


class CheckFailure(RuntimeError):
    pass


def remaining(deadline: float, cap: float = 300) -> float:
    seconds = min(cap, deadline - time.monotonic())
    if seconds <= 0:
        raise RunDeadlineExceeded('HAiKnow run time budget exhausted')
    return seconds


def stop_process(process: subprocess.Popen) -> None:
    """Only stop the process group that this validator created."""
    if os.name == 'posix':
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass
        # A child can outlive npm, even if npm has already exited.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    elif process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
    process.wait(timeout=3)


def tail(path: Path, limit: int = 6000) -> str:
    if not path.exists():
        return ''
    with path.open('rb') as handle:
        handle.seek(0, 2)
        handle.seek(max(0, handle.tell() - limit))
        return handle.read().decode('utf-8', errors='replace')


def validation_env(source: dict[str, str] | None, scratch: Path) -> dict[str, str]:
    """Only controller-created paths survive the credential boundary."""
    env = child_env(source, validation=True)
    for key, leaf in (('HOME', 'home'), ('TMPDIR', 'tmp'),
                      ('npm_config_cache', 'npm-cache'), ('XDG_CACHE_HOME', 'cache')):
        path = scratch / leaf
        path.mkdir(parents=True, exist_ok=True)
        env[key] = str(path.resolve())
    # Fixed organizer image location; never trust an inherited arbitrary path.
    if Path('/ms-playwright').is_dir():
        env['PLAYWRIGHT_BROWSERS_PATH'] = '/ms-playwright'
    return env


def run_command(argv: list[str], cwd: Path, log: Path, deadline: float,
                *, env: dict[str, str] | None = None, cap: float = 300, report_path: Path | None = None, scratch: Path | None = None, browser_cache: Path | None = None) -> dict[str, Any]:
    if scratch is None:
        # Explicit OS root: inherited TMPDIR may itself be inside the product tree.
        with tempfile.TemporaryDirectory(prefix='arc-validation-', dir='/tmp') as temporary:
            return run_command(argv, cwd, log, deadline, env=env, cap=cap,
                               report_path=report_path, scratch=Path(temporary), browser_cache=browser_cache)
    log.parent.mkdir(parents=True, exist_ok=True)
    env = validation_env(env, scratch)
    if browser_cache is not None:
        env['PLAYWRIGHT_BROWSERS_PATH'] = str(browser_cache.resolve())
    # 只放行控制器指定的报告地址，不扩宽共享环境白名单。
    if report_path is not None:
        env['PLAYWRIGHT_JSON_OUTPUT_FILE'] = str(report_path.resolve())
    started = time.monotonic()
    timeout = remaining(deadline, cap)
    print(f'[verify] {cwd.name}: {" ".join(argv[:6])}', flush=True)
    with log.open('w', encoding='utf-8') as handle:
        process = subprocess.Popen(argv, cwd=cwd, env=env, stdout=handle,
                                   stderr=subprocess.STDOUT, start_new_session=True)
        try:
            code = process.wait(timeout=timeout)
        finally:
            stop_process(process)
    record = {'argv': argv, 'cwd': str(cwd), 'exit_code': code,
              'seconds': round(time.monotonic() - started, 3), 'log': str(log)}
    if code:
        raise CheckFailure(f'Command failed ({code}): {argv}\n{tail(log)}')
    return record


def read_test_report(path: Path, kind: str) -> dict[str, int]:
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise CheckFailure(f'Missing or invalid {kind} JSON test report: {path}') from exc
    if not isinstance(data, dict):
        raise CheckFailure(f'Invalid {kind} report object')
    if kind == 'vitest':
        total = data.get('numTotalTests', 0)
        passed = data.get('numPassedTests', 0)
        failed = data.get('numFailedTests', 0)
        skipped = data.get('numPendingTests', 0) + data.get('numTodoTests', 0)
        valid = data.get('success') is True and passed > 0 and failed == 0 and passed == total and skipped == 0
    else:
        stats = data.get('stats') or {}
        passed = stats.get('expected', 0)
        failed = stats.get('unexpected', 0) + stats.get('flaky', 0)
        skipped = stats.get('skipped', 0)
        total = passed + failed + skipped
        valid = passed > 0 and failed == 0 and skipped == 0 and not data.get('errors')
        from core.scenario_coverage import execution_evidence
        _, execution_issues = execution_evidence(data)
        if execution_issues:
            raise CheckFailure('playwright: actual executions must pass (expected failures do not count): '
                               + json.dumps(execution_issues[:10], ensure_ascii=False))
    if not valid:
        raise CheckFailure(f'{kind}: tests must actually run, with no failures/skips. '
                           f'total={total}, passed={passed}, failed={failed}, skipped={skipped}')
    return {'total': int(total), 'passed': int(passed), 'failed': int(failed), 'skipped': int(skipped)}


def audit_scenarios(project, result, evidence, report):
    """The frozen current source defines IDs; prior sources only select deltas."""
    from core.scenario_coverage import audit_report
    current = project / '.haiknow/acceptance.json'
    prior = project / '.haiknow/prior-acceptance.json'
    if current.is_symlink() or prior.is_symlink():
        raise CheckFailure('Scenario sources must not be symlinks')
    try:
        audit = audit_report(json.loads(result.read_text()), json.loads(current.read_text()),
                             json.loads(prior.read_text()) if prior.is_file() else {})
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise CheckFailure('Cannot verify current scenario executions: ' + str(exc)) from exc
    report['scenario_executions'] = audit
    (evidence / 'scenario-executions.json').write_text(json.dumps(audit, ensure_ascii=False, indent=2) + '\n')
    if not audit['passed']:
        raise CheckFailure('Current scenario execution coverage failed: '
                           + json.dumps(audit['issues'][:20], ensure_ascii=False)
                           + '\nUse one source-case annotation per real Playwright test; implement the '
                             'original GIVEN/WHEN/THEN, not just labels. See scenario-executions.json.')


def naming_enabled():
    return profile_enabled()


def load_name_reviews(project):
    path = project / '.haiknow/ui-name-review.json'
    try:
        reviews = json.loads(path.read_text()) if path.exists() else []
    except (OSError, ValueError) as exc:
        raise CheckFailure('Missing or invalid .haiknow/ui-name-review.json: ' + str(exc)) from exc
    if not isinstance(reviews, list) or not all(isinstance(item, dict) for item in reviews):
        raise CheckFailure('ui-name-review.json must be an array of individual explanations')
    return reviews


def audit_names(project, evidence, report, observations):
    supplemental = project / '.haiknow/visual-text.json'
    facts = json.loads(supplemental.read_text()) if supplemental.exists() else []
    acceptance = load_name_acceptance(project)
    audit = check_names(acceptance,
                        observations, load_name_reviews(project), facts)
    report['ui_names'] = audit
    evidence.mkdir(parents=True, exist_ok=True)
    (evidence / 'ui-names.json').write_text(json.dumps(audit, ensure_ascii=False, indent=2) + '\n')
    if audit['issues']:
        raise CheckFailure(failure_message(audit))
    # Retained text still supports names/roles; new locator obligations come
    # only from the current frozen scenarios, not old-only case labels.
    current = json.loads((project / '.haiknow/acceptance.json').read_text())
    semantics = check_semantics(acceptance, observations, scope_acceptance=current)
    report['ui_semantics'] = semantics
    (evidence / 'ui-semantics.json').write_text(json.dumps(semantics, ensure_ascii=False, indent=2) + '\n')
    if semantics['issues']:
        raise CheckFailure('Functional control semantics: ' + '\n'.join(
            semantic_issue_message(i) for i in semantics['issues'][:20]))


SOURCE_EXCLUDED = {'.git', '.arc', '.codex', 'node_modules', 'dist', 'build',
                   '__pycache__', 'coverage', 'test-results', 'playwright-report'}


def source_manifest(project, evidence=None, *, database_primary=None):
    """Inventory the complete cold source, pruning outputs before traversal."""
    manifest = {}
    primary = database_primary or primary_path(project)
    for directory, dirs, files in os.walk(project, followlinks=False):
        base = Path(directory)
        dirs[:] = sorted(name for name in dirs if name not in SOURCE_EXCLUDED and not name.startswith('.env')
                         and (base / name).resolve() != evidence)
        for name in dirs + files:
            path = base / name
            if name in SOURCE_EXCLUDED or name.startswith('.env'):
                continue
            if path.is_symlink():
                raise CheckFailure('Symlink in validation source: ' + str(path.relative_to(project)))
        for name in files:
            if name in SOURCE_EXCLUDED or name.startswith('.env') or name.endswith('.pyc') or is_database_path((base / name).relative_to(project).as_posix(), primary):
                continue
            path = base / name
            manifest[str(path.relative_to(project))] = {
                'sha256': sha256(path.read_bytes()).hexdigest(), 'mode': path.stat().st_mode & 0o777}
    return manifest


def manifest_digest(manifest):
    return sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()


class DetachedSource:
    """Shared cold-copy and drift guard for local checks and native E2E.

    No published source/dependency inode is written. Hashes plus mode and full
    inventory reject edits, additions, removals and symlinks in either source.
    This is boundary detection, not an OS sandbox against hostile same-UID code.
    """
    def __init__(self, project, scratch, evidence):
        self.published, self.evidence = project.resolve(), evidence.resolve()
        self.project = scratch / 'project'
        self.database_primary = primary_path(self.published)
        self.database_manifest = inventory(self.published, self.database_primary)
        self.manifest = source_manifest(self.published, self.evidence, database_primary=self.database_primary)
        self.identities = {name: (self.published/name).stat().st_ino for name in self.manifest}
        self.project.mkdir(parents=True)
        for name in self.manifest:
            destination = self.project/name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(self.published/name, destination)
        self.check()
        evidence.mkdir(parents=True, exist_ok=True)
        (evidence/'source-manifest.json').write_text(json.dumps(self.manifest, sort_keys=True, indent=2)+'\n')

    def check(self):
        if inventory(self.published, self.database_primary) != self.database_manifest:
            raise CheckFailure('Published database changed during detached validation')
        for root in (self.published, self.project):
            current = source_manifest(root, self.evidence, database_primary=self.database_primary)
            changed = sorted(name for name in self.manifest.keys() | current.keys()
                             if self.manifest.get(name) != current.get(name))
            if root == self.published:
                changed += [name for name, inode in self.identities.items()
                            if name in current and (root/name).stat().st_ino != inode]
            if changed:
                raise CheckFailure('Source changed during detached validation: ' + ', '.join(changed[:10]))

    def metadata(self):
        return {'validation_workspace': 'disposable cold source copy outside published project',
                'source_manifest_sha256': manifest_digest(self.manifest),
                'source_file_count': len(self.manifest), 'source_manifest': str(self.evidence/'source-manifest.json')}


def verify_project(project: Path, evidence: Path, deadline: float,
                   *, delivery_only: bool = False, manual_contract_checks: bool = False) -> dict[str, Any]:
    """Validate a cold source copy without reinstalling published dependencies."""
    if manual_contract_checks is not False:
        raise ValueError('Manual contract bindings are not supported')
    project, evidence = project.resolve(), evidence.resolve()
    with tempfile.TemporaryDirectory(prefix='arc-validation-', dir='/tmp') as temporary:
        scratch = Path(temporary)
        snapshot = None
        report = None
        integrity_ok = False
        interrupted = False
        try:
            snapshot = DetachedSource(project, scratch, evidence)
            browser_cache = project/'.arc/haiknow/browser-cache'
            report = _verify_project(snapshot.project, evidence, deadline, scratch,
                                     delivery_only=delivery_only,
                                     **({'browser_cache': browser_cache} if browser_cache.is_dir() else {}))
            if report['passed']:
                snapshot.check()
                report['passed'] = False
                try:
                    report['database_delivery'] = verify_retained(project, snapshot.project, scratch,
                        evidence, deadline, validation_env(None, scratch), run_journeys=not delivery_only)
                    report['passed'] = True
                except DatabaseDeliveryError as exc:
                    report.update(passed=False, failure_kind='business', error=str(exc))
                    detail = evidence / 'database-delivery.json'
                    if detail.exists(): report['database_delivery'] = json.loads(detail.read_text())
            observations = report.pop('ui_name_observations', None)
            if observations is not None and not delivery_only and naming_enabled():
                audit_names(snapshot.project, evidence, report, observations)
        except TimeoutError as exc:
            interrupted = True
            # The inner validator persists completed phases even when interrupted.
            partial = evidence/'validation.json'
            if report is None and partial.is_file():
                report = json.loads(partial.read_text())
            if report is not None:
                report.update(passed=False, error=str(exc))
                database_report = evidence/'database-delivery.json'
                if database_report.is_file(): report['database_delivery'] = json.loads(database_report.read_text())
            raise
        except (CheckFailure, DatabaseDeliveryError) as exc:
            report = report or {'commands': [], 'tests': {}}
            report.update(passed=False, error=str(exc))
        finally:
            if snapshot is not None:
                try:
                    snapshot.check(); integrity_ok = True
                except (CheckFailure, OSError) as exc:
                    report = report or {'commands': [], 'tests': {}}
                    report.update(passed=False, error=str(exc))
            if report is not None:
                report['source_integrity'] = integrity_ok
                if snapshot is not None: report.update(snapshot.metadata())
                evidence.mkdir(parents=True, exist_ok=True)
                (evidence / 'validation.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
            if interrupted and snapshot is not None and not integrity_ok:
                raise CheckFailure('Source changed during interrupted detached validation')
        return report


def _verify_project(project: Path, evidence: Path, deadline: float, scratch: Path,
                    *, delivery_only: bool = False, browser_cache=None,
                    manual_contract_checks: bool = False) -> dict[str, Any]:
    """Install, build, run real tests and check both delivery listeners."""
    if manual_contract_checks is not False:
        raise ValueError('Manual contract bindings are not supported')
    evidence.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {'scope': ('build and dual-port startup only; tests not run' if delivery_only else
                                        'internal delivery checks; not official grading'),
                              'passed': False, 'commands': [], 'tests': {}, 'error': None}
    report['manual_contract_checks'] = 'disabled'
    env = validation_env(None, scratch)
    env.update({'CI': '1', 'NO_COLOR': '1', 'npm_config_audit': 'false', 'npm_config_fund': 'false'})
    env['ARC_DB_FILE'] = str(scratch / 'startup.db')
    env['DATABASE_FILE'] = env['ARC_DB_FILE']
    env.pop('NODE_ENV', None)  # Tests/builds need dev dependencies even in production runners.

    def command(argv: list[str], cwd: Path, name: str, extra: dict[str, str] | None = None, *, cap=300) -> None:
        try:
            report['commands'].append(run_command(argv, cwd, evidence / f'{name}.log', deadline,
                                               env={**env, **(extra or {})}, scratch=scratch, browser_cache=browser_cache, cap=cap,
                                               report_path=Path(extra['PLAYWRIGHT_JSON_OUTPUT_FILE']) if extra and 'PLAYWRIGHT_JSON_OUTPUT_FILE' in extra else None))
        except subprocess.TimeoutExpired:
            # A hanging application build/test needs changed code, not an
            # unbounded identical rerun. Only tooling installation is infrastructure.
            report['failed_operation'] = name
            report['failure_kind'] = 'infrastructure' if name.startswith('install-') else 'business'
            raise
        except CheckFailure:
            # Dependency transport/setup retries are separate. Executed build or
            # generated unit/API/E2E failures require implementation repair.
            if name.startswith(('build-', 'test-')): report['failure_kind'] = 'business'
            raise

    def tests(cwd: Path, name: str, config: str) -> None:
        result = evidence / f'{name}.json'
        result.unlink(missing_ok=True)
        executable = cwd / 'node_modules/vitest/vitest.mjs'
        if not executable.is_file():
            raise CheckFailure(f'Vitest missing in {cwd.name}; preserve template test dependencies')
        command(['node', str(executable), 'run', '--config', config, '--reporter=json',
                 '--outputFile', str(result), '--maxWorkers=1', '--no-file-parallelism'], cwd, name,
                {'ARC_DB_FILE': str(scratch / (name + '.db')), 'DATABASE_FILE': str(scratch / (name + '.db'))})
        try:
            report['tests'][name] = read_test_report(result, 'vitest')
        except CheckFailure:
            report['failure_kind'] = 'business'
            raise

    try:
        if not delivery_only and naming_enabled():
            load_name_reviews(project)  # Invalid shape is cheap; do this before installs.
        for part in ('backend', 'frontend'):
            cwd = project / part
            if not (cwd / 'package.json').is_file():
                raise CheckFailure(f'Missing {part}/package.json; preserve the Web delivery contract')
            # npm ci detects lock drift. Repairs must fix it, not hide the error.
            install = 'ci' if (cwd / 'package-lock.json').is_file() else 'install'
            command(['npm', install, '--include=dev', '--no-audit', '--no-fund'], cwd, f'install-{part}')
        if not delivery_only and naming_enabled():
            names = evidence / 'ui-name-observations.json'
            command(['node', str(Path(__file__).with_name('ui_names.cjs')), str(project), str(names)],
                    project, 'ui-name-extraction')
            audit_names(project, evidence, report, json.loads(names.read_text()))
        command(['npm', 'run', 'build'], project / 'frontend', 'build-frontend')
        if not (project / 'frontend/dist/index.html').is_file():
            raise CheckFailure('Build did not produce frontend/dist/index.html')

        with socket.socket() as port_socket, socket.socket() as second_socket:
            port_socket.bind(('127.0.0.1', 0))
            second_socket.bind(('127.0.0.1', 0))
            port = port_socket.getsockname()[1]
            test_port = second_socket.getsockname()[1]
        base = f'http://127.0.0.1:{port}'
        test_base = f'http://127.0.0.1:{test_port}'
        server_env = {**env, 'PORT': str(port), 'ARC_WEB_PORT': str(port),
                      'ARCBENCH_WEB_PORT': str(port), 'SPEC_BASE_PORTS': f'{port},{test_port}',
                      'PLAYWRIGHT_BASE_URL': test_base, 'ARC_WEB_BASE_URL': test_base}
        opener = build_opener(ProxyHandler({}))
        with (evidence / 'server.log').open('w', encoding='utf-8') as log:
            server = subprocess.Popen(['npm', 'run', 'start'], cwd=project / 'backend', env=server_env,
                                      stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            try:
                ready_deadline = min(deadline, time.monotonic() + 45)
                ready = False
                while time.monotonic() < ready_deadline and server.poll() is None:
                    try:
                        with opener.open(base + '/api/health', timeout=2) as response:
                            ready = response.status == 200
                        if ready:
                            break
                    except (URLError, OSError, TimeoutError):
                        pass
                    time.sleep(.25)
                if not ready:
                    raise CheckFailure('Backend did not become healthy:\n' + tail(evidence / 'server.log'))
                for listener_port in (port, test_port):
                    listener_base = f'http://127.0.0.1:{listener_port}'
                    try:
                        with opener.open(listener_base + '/api/health', timeout=2) as response:
                            if response.status != 200:
                                raise CheckFailure('Health endpoint did not return 200')
                        with opener.open(listener_base + '/', timeout=5) as response:
                            content = response.read(1024 * 1024).decode('utf-8', errors='replace')
                            if response.status != 200 or '<html' not in content.lower() or 'frontend build missing' in content.lower():
                                raise CheckFailure('Root page is not a built HTML application')
                    except (URLError, OSError, TimeoutError, CheckFailure) as exc:
                        raise CheckFailure(f'Delivery port {listener_port} failed: {exc}. '
                                           'Serve the same app on every SPEC_BASE_PORTS listener; '
                                           'default to PORT (3000) and 3301.') from exc
                report['startup'] = {'health': 200, 'page': 200, 'port': port,
                                     'ports': [port, test_port], 'e2e_port': test_port}
                if not delivery_only:
                    # Unit suites may mutate databases, too. Inspect cold startup first.
                    tests(project / 'frontend', 'test-frontend', 'vite.config.js')
                    tests(project / 'backend', 'test-backend', 'vitest.config.js')
                    backend = project / 'backend'
                    executable = backend / 'node_modules/@playwright/test/cli.js'
                    if not executable.is_file():
                        raise CheckFailure('Missing @playwright/test; preserve E2E dependencies')
                    result = evidence / 'test-e2e.json'
                    e2e_env = {**server_env, 'PLAYWRIGHT_JSON_OUTPUT_FILE': str(result)}
                    # First use the installed browser. Only fetch Chromium if Playwright explicitly lacks it.
                    for attempt in range(2):
                        result.unlink(missing_ok=True)
                        try:
                            command(['node', str(executable), 'test', '--config', 'playwright.config.js',
                                     '--reporter=json', '--workers=1', '--forbid-only'], backend, f'test-e2e-{attempt}', e2e_env)
                            break
                        except CheckFailure as exc:
                            failure = str(exc) + tail(result)
                            if attempt == 0 and ('Executable doesn\'t exist' in failure or 'executable doesn\'t exist' in failure):
                                browser_cache = scratch / 'cache/ms-playwright'
                                browser_cache.mkdir(parents=True, exist_ok=True)
                                command(['node', str(executable), 'install', 'chromium'], backend, 'install-browser')
                            else:
                                raise
                    try:
                        report['tests']['test-e2e'] = read_test_report(result, 'playwright')
                        if profile_enabled():
                            audit_scenarios(project, result, evidence, report)
                    except CheckFailure:
                        report['failure_kind'] = 'business'
                        raise
                if server.poll() is not None:
                    raise CheckFailure('Backend exited during E2E checks')
            finally:
                stop_process(server)
        report['passed'] = True
    except TimeoutError:
        # 上抛给控制器区分工作截止与最终截止，不当成普通测试失败重试。
        report['error'] = 'TimeoutError: explicit run deadline'
        raise
    except Exception as exc:
        report['error'] = f'{type(exc).__name__}: {exc}'
    finally:
        (evidence / 'validation.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    return report


def verify_delivery(project: Path, evidence: Path, deadline: float) -> dict[str, Any]:
    """修复耗尽后的独立可运行检查；不代表内部测试或官方测试通过。"""
    return verify_project(project, evidence, deadline, delivery_only=True)

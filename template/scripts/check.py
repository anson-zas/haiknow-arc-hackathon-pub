#!/usr/bin/env python3
"""Development checks with owned resources and reusable successful evidence.

Final controller acceptance deliberately does not consume this cache. No official
or external test input, arbitrary commands, network lookup or model call is used.
"""
import argparse
from contextlib import contextmanager
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from urllib.request import build_opener, ProxyHandler
from urllib.error import URLError

EXCLUDED = {'.git', '.arc', '.codex', 'node_modules', 'dist', 'build', 'coverage',
            'test-results', 'playwright-report', '__pycache__', '.arc-test-db'}
TARGETS = ('build', 'frontend', 'backend', 'e2e')


def digest(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def inventory(root, *, prune=True, ignored_files=()):
    result = {}
    if not root.is_dir():
        return result
    for directory, dirs, files in os.walk(root, followlinks=False):
        base = Path(directory)
        dirs[:] = sorted(n for n in dirs if not prune or n not in EXCLUDED)
        for name in dirs + files:
            if (base/name).is_symlink():
                raise ValueError('Unsupported symlink in source/artifact: '+str(base/name))
        for name in sorted(files):
            # Runner outputs live in excluded directories. Database/log fixtures
            # elsewhere may be legitimate test inputs and must affect identity.
            if name.endswith('.pyc'):
                continue
            path = base/name
            relative = path.relative_to(root).as_posix()
            if relative in ignored_files:
                continue
            result[relative] = sha256(path.read_bytes()).hexdigest()
    return result


def source_identity(root):
    # Shared modules, public assets and root configuration can affect every suite.
    # The controller's progress journal records completed work, not app inputs.
    # Keep all other knowledge files, Markdown and fixtures in the identity.
    return inventory(root, ignored_files={'.haiknow/progress.md'})


def dependency_inventory(root):
    """Detect missing/changed installed files without hashing dependency bodies."""
    result = {}
    for directory, dirs, files in os.walk(root, followlinks=False):
        base = Path(directory)
        # Vite/Vitest rewrite their output caches during a normal successful run.
        if base == root:
            dirs[:] = sorted(n for n in dirs if n not in {'.vite', '.vite-temp', '.cache'})
        for name in sorted(dirs + files):
            path = base/name
            if path.is_dir() and not path.is_symlink(): continue
            stat = path.lstat()
            result[str(path.relative_to(root))] = [stat.st_size, stat.st_mtime_ns, stat.st_mode,
                                                  os.readlink(path) if path.is_symlink() else None]
    return result


def environment_identity(env, root):
    # Omit only values owned by this runner and irrelevant shell bookkeeping.
    # Unknown application flags are included by default, including their values
    # only inside a digest (never written to reports).
    owned = {'PWD', 'OLDPWD', '_', 'SHLVL'}
    runtime = {}
    for name in ('node', 'npm'):
        path = shutil.which(name, path=env.get('PATH'))
        if not path:
            raise ValueError(name+' is unavailable')
        p = Path(path).resolve()
        runtime[name] = [str(p), p.stat().st_size, p.stat().st_mtime_ns]
    installed = {}
    for part in ('frontend', 'backend'):
        lock = root/part/'node_modules/.package-lock.json'
        installed[part] = {'lock':sha256(lock.read_bytes()).hexdigest() if lock.is_file() else None,
                          'files':dependency_inventory(root/part/'node_modules')}
    return digest({'env': {k:v for k,v in env.items() if k not in owned},
                   'platform':sys.platform, 'runtime':runtime, 'installed':installed})


def stop(process):
    if os.name == 'posix':
        try: os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError: pass
        try: process.wait(timeout=2)
        except subprocess.TimeoutExpired: pass
        try: os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError: pass
    elif process.poll() is None:
        process.terminate()
        try: process.wait(timeout=2)
        except subprocess.TimeoutExpired: process.kill()
    process.wait(timeout=3)


def run(argv, cwd, env, log, timeout):
    with log.open('w') as handle:
        process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
            stdout=handle, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            code = process.wait(timeout=timeout)
            if code:
                raise RuntimeError('Command failed with exit '+str(code)+': '+str(log))
        finally:
            stop(process)


@contextmanager
def server(root, env, work, startup_timeout):
    # Reserve two distinct ports together. Release immediately before launching;
    # a competing bind is a failed startup, never permission to kill its owner.
    held = [socket.socket(), socket.socket()]
    try:
        for sock in held: sock.bind(('127.0.0.1', 0))
        ports = [sock.getsockname()[1] for sock in held]
    finally:
        for sock in held: sock.close()
    env.update(PORT=str(ports[0]), SPEC_BASE_PORTS=','.join(map(str, ports)),
               ARC_DB_FILE=str(work/'app.sqlite'), PLAYWRIGHT_BASE_URL=f'http://127.0.0.1:{ports[1]}',
               ARC_WEB_BASE_URL=f'http://127.0.0.1:{ports[1]}')
    with (work/'server.log').open('w') as log:
        process = subprocess.Popen(['node', 'src/index.js'], cwd=root/'backend', env=env,
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            deadline = time.monotonic()+startup_timeout
            opener = build_opener(ProxyHandler({}))
            ready = set()
            while len(ready) < 2:
                if process.poll() is not None:
                    raise RuntimeError('Backend exited before readiness; see '+str(work/'server.log'))
                if time.monotonic() >= deadline:
                    raise TimeoutError('Backend dual-port readiness timeout; see '+str(work/'server.log'))
                for port in ports:
                    if port in ready: continue
                    try:
                        with opener.open(f'http://127.0.0.1:{port}/api/health', timeout=.2) as response:
                            if response.status == 200: ready.add(port)
                    except (OSError, URLError): pass
                if len(ready) < 2: time.sleep(.04)
            yield
        finally:
            stop(process)


def counts(path, target):
    data = json.loads(path.read_text())
    if target == 'e2e':
        stats = data.get('stats', {})
        passed, failed, skipped = (stats.get('expected',0), stats.get('unexpected',0)+stats.get('flaky',0), stats.get('skipped',0))
        total = passed+failed+skipped
        success = not data.get('errors')
    else:
        total, passed, failed, skipped = (data.get('numTotalTests',0), data.get('numPassedTests',0), data.get('numFailedTests',0), data.get('numPendingTests',0)+data.get('numTodoTests',0))
        success = data.get('success') is True
    if not all(type(n) is int and n >= 0 for n in (total,passed,failed,skipped)):
        raise ValueError('Invalid test counts')
    if not (success and total > 0 and passed == total and failed == skipped == 0):
        raise ValueError('Tests must execute without failures, skips or flakes')
    return {'total':total, 'passed':passed, 'failed':failed, 'skipped':skipped}


def failure_diagnostics(report, log, server_log):
    """Read bounded existing evidence, without retrying or changing test status."""
    messages = []
    def visit(value, context=()):
        if len(messages) >= 3:
            return
        if isinstance(value, dict):
            name = next((value[k] for k in ('fullName','title','name','file')
                         if isinstance(value.get(k), str) and value[k]), '')
            owners = (*context, name)[-3:] if name else context
            texts = [value.get('message')]
            if isinstance(value.get('failureMessages'), list):
                texts += value['failureMessages']
            for message in texts:
                if isinstance(message, str) and message.strip() and len(messages) < 3:
                    entry = (' > '.join(owners)[-400:]+'\n'+message.strip()[:900]).strip()
                    if entry not in messages:
                        messages.append(entry)
            for key, child in value.items():
                if key not in ('message','failureMessages'):
                    visit(child, owners)
        elif isinstance(value, list):
            for child in value:
                visit(child, context)
    try:
        if report.stat().st_size <= 4 * 1024 * 1024:
            visit(json.loads(report.read_text()))
    except (OSError, ValueError, RecursionError):
        pass
    if messages:
        return '\n\n'.join(messages)[:3600]
    # Startup/build failures or malformed reports still retain their real logs.
    for path in (log, server_log):
        try:
            with path.open('rb') as stream:
                stream.seek(max(0, path.stat().st_size-4096))
                tail = stream.read(4096).decode('utf-8', errors='replace').strip()
            if tail:
                return str(path)+'\n'+tail[-3000:]
        except OSError:
            pass
    return 'No diagnostic detail captured; inspect the preserved report and logs.'


def execute(root, targets, files, fresh, timeout, startup_timeout):
    root = root.resolve()
    base = root/'.arc/local-check'; base.mkdir(parents=True, exist_ok=True)
    cache = base/'cache'; cache.mkdir(exist_ok=True)
    # Independent invocations share evidence; serialize to prevent duplicate
    # side effects and stale cache writes from concurrent checks.
    import fcntl
    with (base/'lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        env = dict(os.environ)
        # Controller installs this matching browser before the coding phase.
        browser = root/'.arc/haiknow/browser-cache'
        if browser.is_dir(): env['PLAYWRIGHT_BROWSERS_PATH'] = str(browser)
        env_key = environment_identity(env, root)
        requested = list(dict.fromkeys(targets))
        if 'e2e' in requested and 'build' not in requested:
            requested.insert(requested.index('e2e'), 'build')
        elif 'e2e' in requested:
            requested.remove('build'); requested.insert(requested.index('e2e'), 'build')
        report = {'scope':'development-only', 'passed':False, 'targets':{}}
        for target in requested:
            selected = files if target != 'build' else []
            before = source_identity(root)
            key = digest({'source':before, 'target':target, 'files':selected, 'environment':env_key,
                'served_build':inventory(root/'frontend/dist',prune=False) if target=='e2e' else None,
                'runner':sha256(Path(__file__).read_bytes()).hexdigest()})
            record_path = cache/(key+'.json')
            try: prior = json.loads(record_path.read_text())
            except (OSError, ValueError): prior = None
            valid = False
            if not fresh and isinstance(prior,dict) and prior.get('passed') is True:
                try:
                    evidence = Path(prior['log'] if target=='build' else prior['report'])
                    valid = evidence.is_relative_to(base) and sha256(evidence.read_bytes()).hexdigest() == prior['evidence_sha256']
                    if target=='build': valid = valid and bool(prior.get('artifacts')) and inventory(root/'frontend/dist', prune=False)==prior['artifacts']
                    else: valid = valid and counts(evidence,target)==prior['counts']
                except (OSError, ValueError, KeyError, TypeError): valid=False
            if valid:
                report['targets'][target] = {**prior, 'cached':True}; continue
            # A fresh failure/cancellation must invalidate older success for
            # this identity, otherwise the next normal call could hide it.
            record_path.unlink(missing_ok=True)
            work = Path(tempfile.mkdtemp(prefix=target+'-',dir=base))
            (work/'tmp').mkdir(); child = {**env, 'TMPDIR':str(work/'tmp')}
            log=work/'command.log'; test_report=work/'tests.json'; started=time.monotonic()
            try:
                if target=='build':
                    run(['npm','run','build'],root/'frontend',child,log,timeout)
                    artifacts=inventory(root/'frontend/dist',prune=False)
                    if 'index.html' not in artifacts: raise ValueError('Build did not produce frontend/dist/index.html')
                    result={'artifacts':artifacts}; evidence=log
                else:
                    if target=='e2e':
                        child['PLAYWRIGHT_JSON_OUTPUT_FILE']=str(test_report)
                        with server(root,child,work,startup_timeout):
                            run(['npm','run','test:e2e','--',*selected,'--reporter=line,json'],root/'backend',child,log,timeout)
                    else:
                        run(['npm','run','test','--',*selected,'--reporter=default','--reporter=json','--outputFile='+str(test_report)],root/target,child,log,timeout)
                    result={'counts':counts(test_report,target),'report':str(test_report)};evidence=test_report
                if source_identity(root)!=before:
                    raise ValueError('Source changed during checks; result cannot be reused')
                result.update(passed=True,cached=False,log=str(log),seconds=round(time.monotonic()-started,3),evidence_sha256=sha256(evidence.read_bytes()).hexdigest(),identity=key)
                record_path.write_text(json.dumps(result,sort_keys=True)+'\n')
                report['targets'][target]=result
            except BaseException as exc:
                detail=failure_diagnostics(test_report, log, work/'server.log')
                report['targets'][target]={'passed':False,'cached':False,'log':str(log),
                    'report':str(test_report),'diagnostics':detail,
                    'error':type(exc).__name__+': '+str(exc)}
                (base/'latest.json').write_text(json.dumps(report,indent=2)+'\n')
                print('Failed '+target+'; report: '+str(test_report)+'\n'+detail,file=sys.stderr)
                raise
        report['passed']=True
        (base/'latest.json').write_text(json.dumps(report,indent=2)+'\n')
        return report


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('targets',nargs='+',choices=TARGETS)
    parser.add_argument('--project-dir',type=Path,default=Path(__file__).resolve().parents[1])
    parser.add_argument('--file',action='append',default=[],help='Relative file in the selected test suite')
    parser.add_argument('--fresh',action='store_true',help='Execute even if unchanged success is cached')
    parser.add_argument('--timeout',type=float,default=300)
    parser.add_argument('--startup-timeout',type=float,default=30)
    args=parser.parse_args(argv)
    if args.timeout<=0 or args.startup_timeout<=0: parser.error('Timeouts must be positive')
    if args.file and (len(args.targets)!=1 or args.targets[0]=='build'):
        parser.error('--file requires one test target')
    for filename in args.file:
        path=Path(filename)
        if path.is_absolute() or '..' in path.parts or filename.startswith('-'):
            parser.error('Test files must be relative paths within their suite')
    def cancelled(signum, frame): raise KeyboardInterrupt('Cancelled by signal '+str(signum))
    signal.signal(signal.SIGTERM,cancelled)
    try:
        print(json.dumps(execute(args.project_dir,args.targets,args.file,args.fresh,args.timeout,args.startup_timeout),sort_keys=True))
        return 0
    except (Exception,KeyboardInterrupt) as exc:
        print(type(exc).__name__+': '+str(exc),file=sys.stderr)
        return 130 if isinstance(exc,KeyboardInterrupt) else 1


if __name__=='__main__':sys.exit(main())

"""Landrun confines SDK processes to project writes; evaluator access is an input policy."""
from hashlib import sha256
import json
import os
from pathlib import Path
import platform
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT/'competition/evaluator-isolation.json'
VENDOR = ROOT/'vendor/landrun'


class EvaluatorIsolationError(RuntimeError):
    """An infrastructure failure, never a business/model repair opportunity."""


def evaluator_paths(evaluator_roots=None):
    values = (json.loads(CONFIG.read_text()).get('evaluator_roots', [])
              if evaluator_roots is None else evaluator_roots)
    if not isinstance(values, list):
        raise EvaluatorIsolationError('Evaluator roots must be an absolute-path list')
    paths = set()
    for value in values:
        if not isinstance(value, str) or not Path(value).is_absolute() or '..' in Path(value).parts or value == '/':
            raise EvaluatorIsolationError('Invalid evaluator root')
        # Evaluator roots are policy labels, including when absent. Resolving
        # symlinks would query undisclosed evaluator filesystem metadata.
        paths.add(str(Path(value).absolute()))
    return sorted(paths)


def access_policy(evaluator_roots=None):
    config = json.loads(CONFIG.read_text())
    if not isinstance(config, dict):
        raise EvaluatorIsolationError('Evaluator access config must be an object')
    mode = config.get('official_test_access', 'deny')
    if mode != 'deny':
        raise EvaluatorIsolationError('This competition package requires official_test_access=deny')
    return {'official_test_access': mode,
            'evaluator_roots': evaluator_paths(config.get('evaluator_roots') if evaluator_roots is None else evaluator_roots),
            'unknown_task_eligible': mode == 'deny', 'enforcement': 'agent_input_policy',
            'os_read_denied': False, 'write_boundary': 'Landlock: project only; /dev/null device exception'}


def access_instructions():
    policy = access_policy()
    text = 'official_test_access='+policy['official_test_access']+'.\n'
    if policy['evaluator_roots']:
        text += 'Configured evaluator roots: '+json.dumps(policy['evaluator_roots'])+'.\n'
    text += ('Do not actively discover or inspect undisclosed tests or evaluator locations: never read or execute their tests/helpers. '
             'Do not check their existence, stat/lstat them, list directories, search, or resolve their symlinks. '
             'This prohibition applies whether or not evaluator locations are known. '
             'This is an input-use rule, not OS read isolation.\n')
    return text + ('Never modify or delete official tests/helpers or evaluator originals. '
                   'Write application E2E tests from the original YAML.\n')


def landrun_executable(project):
    """Verify the pinned upstream asset and stage it inside the writable project."""
    if platform.system() != 'Linux':
        raise EvaluatorIsolationError('Landrun requires Linux; this host cannot prove Landlock enforcement')
    arch = {'x86_64': 'amd64', 'aarch64': 'arm64', 'arm64': 'arm64'}.get(platform.machine())
    if not arch: raise EvaluatorIsolationError('Unsupported Linux architecture for bundled Landrun')
    manifest = json.loads((VENDOR/'manifest.json').read_text())
    name = 'landrun-linux-'+arch
    entry = next(item for item in manifest['assets'] if item['file'] == name)
    payload = (VENDOR/name).read_bytes()
    if len(payload) != entry['bytes'] or sha256(payload).hexdigest() != entry['sha256']:
        raise EvaluatorIsolationError('Bundled Landrun integrity mismatch')
    target = Path(project).resolve()/'.arc/landrun'/name
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink(): raise EvaluatorIsolationError('Landrun destination must not be a symlink')
    target.write_bytes(payload); target.chmod(0o755)
    return target


def check_project_boundary(project, paths):
    project = Path(project).absolute()
    if any(project.is_relative_to(Path(p)) or Path(p).is_relative_to(project) for p in paths):
        raise EvaluatorIsolationError('Evaluator roots must be outside the writable project')
    project = project.resolve()
    if any(project.is_relative_to(Path(p)) or Path(p).is_relative_to(project) for p in paths):
        raise EvaluatorIsolationError('Evaluator roots must be outside the writable project')


def configure_launcher(config, project, role):
    """Use SDK's supported argv override; retain original flags and bundled tools."""
    from codex_cli_bin import bundled_codex_path, bundled_path_dir
    if role != 'coding': raise EvaluatorIsolationError('Unknown SDK role')
    project = Path(project).resolve()
    policy = access_policy()
    check_project_boundary(project, policy['evaluator_roots'])
    launcher = landrun_executable(project)
    bundled = bundled_path_dir()
    if bundled: config.env['PATH'] = str(bundled)+os.pathsep+config.env.get('PATH', os.defpath)
    command = [str(bundled_codex_path())]
    for setting in config.config_overrides: command.extend(['--config', setting])
    command.extend(['app-server', '--listen', 'stdio://'])
    args = [str(launcher), '--rox', '/', '--rwx', str(project), '--rw', '/dev/null',
            '--unrestricted-network', '--unrestricted-scoped', '--best-effort']
    for key in sorted(config.env): args.extend(['--env', key])
    config.launch_args_override = tuple(args+['--']+command)
    return config


def start_isolated_thread(codex, role, evidence, **kwargs):
    """Inner SDK uses template execution mode; Landrun is the outer write boundary."""
    from openai_codex import Sandbox
    if kwargs.get('sandbox') is not None or kwargs.get('sandboxPolicy') is not None:
        raise EvaluatorIsolationError('SDK sandbox mode is controller-owned')
    client = codex._client
    launch = client.config.launch_args_override
    if not launch or '--rwx' not in launch or '--best-effort' not in launch or '--unrestricted-filesystem' in launch:
        raise EvaluatorIsolationError('SDK was not launched through the configured Landrun boundary')
    request = client._request_raw
    observed = []
    def checked(method, params=None, **options):
        result = request(method, params, **options)
        if method == 'thread/start':
            sandbox = result.get('sandbox')
            if not isinstance(sandbox, dict) or sandbox.get('type') != 'dangerFullAccess' or result.get('approvalPolicy') != 'never':
                raise EvaluatorIsolationError('Unexpected inner SDK execution mode or approval policy')
            record = {'role': role, 'launcher': 'landrun-v0.1.17', 'approval_policy': 'never',
                      'sandbox_type': 'dangerFullAccess', 'evaluator_access': access_policy()}
            Path(evidence).mkdir(parents=True, exist_ok=True)
            (Path(evidence)/'permissions.json').write_text(json.dumps(record, indent=2)+'\n')
            observed.append(record)
        return result
    client._request_raw = checked
    try:
        thread = codex.thread_start(sandbox=Sandbox.full_access, **kwargs)
        if not observed: raise EvaluatorIsolationError('SDK did not expose its execution mode')
        return thread
    finally: client._request_raw = request


def preflight(project, evidence, requirements_file, deadline, *, evaluator_roots=None, node_modules=None, browser_cache=None):
    """No model request; exercise the actual SDK process under Landrun on Linux."""
    from types import SimpleNamespace
    from openai_codex import ApprovalMode, Codex, CodexConfig
    from arcbench_security import isolated_sdk
    from main import _codex_config, _redact
    def diagnostic(value, *, tail=False):
        text = _redact(value).replace('unusable-local-isolation-probe', '[REDACTED]')
        return text[-2000:] if tail else text[:2000]
    project, evidence = Path(project), Path(evidence)
    policy = access_policy(evaluator_roots)
    evidence.mkdir(parents=True, exist_ok=True)
    record = {'status': 'failed', 'model_requests': 0, **policy, 'checks': {},
              'stage': 'browser' if node_modules else 'core', 'boundary': 'SDK process and descendants',
              'launcher': 'landrun-v0.1.17', 'platform': platform.system()}
    codex = None
    try:
        check_project_boundary(project, policy['evaluator_roots'])
        if platform.system() != 'Linux':
            raise EvaluatorIsolationError('Landrun preflight requires Linux; enforcement is unverified on this host')
        with tempfile.TemporaryDirectory(prefix='landrun-probe-', dir=evidence) as scratch:
            scratch=Path(scratch); work=scratch/'project'; work.mkdir()
            outside=scratch/'outside'; outside.mkdir()
            witness=outside/'original'; witness.write_text('synthetic read-only fixture')
            (work/'outside-link').symlink_to(witness)
            (work/'package.json').write_text(json.dumps({'scripts': {'test': 'node -e "process.exit(0)"'}}))
            fake=SimpleNamespace(base_url='http://127.0.0.1:9/v1',local_key='unusable-local-isolation-probe')
            config=_codex_config(CodexConfig,work/'sdk-home',work,fake)
            codex=Codex(config=config)
            with isolated_sdk(lambda: codex, config.env):
                metadata=codex.metadata.model_dump(mode='json')
                record['cli']={k:metadata[k] for k in ('serverInfo','userAgent','platformFamily','platformOs') if k in metadata}
                start_isolated_thread(codex,'coding',evidence,cwd=str(work),model='isolation-preflight',
                                      ephemeral=True,approval_mode=ApprovalMode.deny_all)
                seconds=deadline-time.monotonic()
                if seconds <= 0: raise TimeoutError('Run deadline reached during Landrun preflight')
                args={'witness':str(witness),'link':str(work/'outside-link'),
                      'requirements':str(Path(requirements_file).resolve()),
                      'node_modules':str(Path(node_modules).resolve()) if node_modules else None,
                      'browser_cache':str(Path(browser_cache).resolve()) if browser_cache else None,
                      'browser_path':os.environ.get('E2E_BROWSER_PATH')}
                response=codex._client._request_raw('command/exec',{
                    'command':['python3',str(ROOT/'core/evaluator_probe.py'),json.dumps(args)],
                    'cwd':str(work),'timeoutMs':int(min(seconds,90)*1000)})
                try: record['checks']=json.loads(response.get('stdout',''))
                except (ValueError,TypeError): pass
                record['exit_code']=response.get('exitCode')
                record['stderr']=diagnostic(response.get('stderr',''),tail=True)
                if response.get('exitCode') != 0 or not record['checks'] or not all(v is True for v in record['checks'].values()):
                    raise EvaluatorIsolationError('Landrun lacks required '+record['stage']+' write-boundary/tool capabilities')
            if witness.read_text() != 'synthetic read-only fixture':
                raise EvaluatorIsolationError('External synthetic file was changed')
        record['status']='passed'
        return record
    except (EvaluatorIsolationError,TimeoutError) as exc:
        record['error']=diagnostic(exc)
        raise
    except Exception as exc:
        record['error']='Landrun preflight failed: '+type(exc).__name__+': '+diagnostic(exc)
        raise EvaluatorIsolationError(record['error']) from None
    finally:
        if codex is not None:
            record['launcher_stderr']=diagnostic('\n'.join(codex._client._stderr_lines),tail=True)
        (evidence/'result.json').write_text(json.dumps(record,indent=2)+'\n')

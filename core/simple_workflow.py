"""Requirement-module implementation, verification and bounded repair workflow."""
import json
import os
from pathlib import Path
import time

from core.runtime_config import derive_limits, resolve_model, transport_limits
from core.simple_recovery import recover


def run_simple(requirements_dir, output_dir, *, policy, started=None):
    from main import (initialize_project, load_root_modules, copy_skills_to_output,
                      implement_modules, time_limit, _write_json, _redact)
    from core.evaluator_isolation import access_policy, preflight
    from core.dependencies import prepare_dependencies
    from core.workspace import prepare_workspace

    started = time.monotonic() if started is None else started
    for key in ('OPENAI_API_KEY', 'OPENAI_BASE_URL', 'MODEL'):
        if not os.environ.get(key, '').strip():
            raise ValueError(f'Missing platform environment variable: {key}')
    os.environ['HAIKNOW_PROFILE'] = policy['profile']
    limits = derive_limits(policy, (requirements_dir/'requirements.yaml').read_bytes())
    modules = load_root_modules(requirements_dir)  # Reject invalid YAML before creating output.
    model = resolve_model(policy)
    access_policy()  # Enforce the evaluator access policy before initialization.
    deadline = float('inf') if limits['total_seconds'] is None else started + limits['total_seconds']
    def available():
        if deadline == float('inf'):
            return None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('Configured requirement-scaled time limit reached')
        return remaining

    agent_root = Path(__file__).resolve().parents[1]
    workspace = prepare_workspace(output_dir, requirements_dir/'requirements.yaml')
    # The platform has already injected the baseline. Missing starter files may
    # be intentional in that application; only an empty workspace needs them.
    if workspace['mode'] == 'fresh':
        initialize_project(agent_root, output_dir)
    skills = copy_skills_to_output(agent_root/'skills', output_dir)
    evidence = output_dir/'.arc/haiknow'
    evidence.mkdir(parents=True, exist_ok=True)
    state = {'workflow':'simple', 'status':'running', 'phase':'isolation_preflight',
             'internal_acceptance_passed':False, 'gate_passed':None,
             'e2e_source':'generated_with_application', 'limits':limits,
             'model':model, 'evaluator_access':access_policy(), 'turns':[], 'validations':[]}
    _write_json(evidence/'state.json', state)
    try:
        preparation_budget = available()
        if preparation_budget is not None:
            preparation_budget -= limits['delivery_reserve_seconds']
        with time_limit(preparation_budget):
            # Preserve both real SDK checks on the existing app or fresh starter.
            preflight(output_dir, evidence/'isolation/core', requirements_dir/'requirements.yaml', deadline)
            state['phase']='tooling'; _write_json(evidence/'state.json',state)
            node_modules, _ = recover('dependencies', lambda _: prepare_dependencies(evidence, deadline, project=output_dir),
                                      evidence, deadline, redact=_redact)
            state['phase']='isolation_preflight'; _write_json(evidence/'state.json',state)
            preflight(output_dir, evidence/'isolation/browser', requirements_dir/'requirements.yaml',
                      deadline, node_modules=node_modules,
                      browser_cache=evidence/'browser-cache' if (evidence/'browser-cache').is_dir() else None)
        state['phase']='implementation'; _write_json(evidence/'state.json',state)
        implement_modules(requirements_dir, output_dir, modules, skills,
            time_budget=available(), repair_attempts=limits['max_business_repair_rounds'],
            model=model, run_limits=limits,
            coding_idle_seconds=transport_limits(policy)['coding_idle_seconds'])
    except BaseException as exc:
        # A coding failure already has richer turn/validation evidence; keep it.
        current = json.loads((evidence/'state.json').read_text())
        if current.get('status') != 'failed':
            current.update(status='failed', error=_redact(f'{type(exc).__name__}: {exc}'))
            _write_json(evidence/'state.json',current)
        raise

"""Configuration for one coding thread and requirement-scaled delivery limits."""
from decimal import Decimal
import json
import os
from pathlib import Path
import yaml

POLICY = Path(__file__).resolve().parents[1] / 'runtime-policy.yaml'
COMPETITION_POLICY = POLICY.parent / 'competition/policy.json'


def transport_limits(policy):
    limits = policy.get('transport', {'coding_idle_seconds': 600})
    if not isinstance(limits, dict) or set(limits) != {'coding_idle_seconds'}:
        raise ValueError('Unknown transport setting')
    for number in limits.values():
        if number is not None and (type(number) not in (int, float) or
                not Decimal(str(number)).is_finite() or number <= 0):
            raise ValueError('Transport idle seconds must be positive finite numbers or null')
    return dict(limits)


def load_policy(path=POLICY):
    value = yaml.safe_load(Path(path).read_text())
    fields = {'schema_version', 'workflow', 'model', 'profile', 'limits'}
    if not isinstance(value, dict) or not fields <= set(value) or set(value) - fields - {'transport'}:
        raise ValueError('Unexpected runtime policy fields')
    value['transport'] = transport_limits(value)
    if value['schema_version'] != 2 or value['workflow'] != 'simple' or value['profile'] not in {'arc-bench', 'none'}:
        raise ValueError('Unsupported workflow/profile/schema')
    selection = value['model']
    if not isinstance(selection, dict) or set(selection) not in ({'model'}, {'model_env'}):
        raise ValueError('Exactly one model/model_env required')
    if not all(isinstance(v, str) and v.strip() for v in selection.values()):
        raise ValueError('Empty model selection')
    limits = value['limits']
    if not isinstance(limits, dict) or not {'time', 'max_business_repair_rounds'} <= set(limits) or set(limits) - {'time', 'max_business_repair_rounds', 'delivery_reserve'}:
        raise ValueError('Unknown limit setting')
    reserve = limits.get('delivery_reserve')
    if reserve is not None:
        if not isinstance(reserve, dict) or set(reserve) != {'fraction', 'minimum_minutes', 'maximum_minutes'}:
            raise ValueError('Unknown delivery reserve setting')
        if any(type(n) not in (int, float) or not Decimal(str(n)).is_finite() or n <= 0 for n in reserve.values()):
            raise ValueError('Delivery reserve values must be positive finite numbers')
        if reserve['fraction'] >= 1 or reserve['minimum_minutes'] > reserve['maximum_minutes']:
            raise ValueError('Delivery reserve requires fraction < 1 and minimum <= maximum')
    rounds = limits['max_business_repair_rounds']
    if rounds is not None and (type(rounds) is not int or rounds < 0):
        raise ValueError('Repair rounds must be a nonnegative integer or null')
    timer = limits['time']
    if timer is not None:
        if not isinstance(timer, dict) or set(timer) != {'minutes_per_requirement', 'minimum_minutes'}:
            raise ValueError('Time must be null or requirement-scaled configuration')
        for number in timer.values():
            if isinstance(number, bool) or not isinstance(number, (int, float)) or not Decimal(str(number)).is_finite() or number <= 0:
                raise ValueError('Time multiplier/floor must be positive finite numbers')
    return value


def resolve_model(policy, env=None):
    env = os.environ if env is None else env
    selection = policy['model']
    model = selection.get('model') or env.get(selection.get('model_env'), '')
    if not isinstance(model, str) or not model.strip():
        raise ValueError('Missing coding model')
    return model.strip()


def derive_limits(policy, raw):
    from core.requirement_sources import compile_source_pack
    allow_external = (policy.get('profile') == 'arc-bench' and
        json.loads(COMPETITION_POLICY.read_text()).get('external_dependencies_as_prerequisites') is True)
    pack = compile_source_pack(raw, allow_external_dependencies=allow_external)
    if 'ROOT' not in pack['requirements'] or pack['requirements']['ROOT']['ancestors']:
        raise ValueError('Original requirements must have ROOT')
    ids = sorted(rid for rid, req in pack['requirements'].items() if rid != 'ROOT' and req['leaf'])
    if not ids:
        raise ValueError('Original requirements must contain leaf requirements')
    timer = policy['limits']['time']
    seconds = None if timer is None else 60 * max(timer['minimum_minutes'], timer['minutes_per_requirement'] * len(ids))
    reserve = policy['limits'].get('delivery_reserve')
    # Very short fixture budgets keep at least half for work; never extra time.
    reserve_seconds = 0
    if seconds is not None and reserve:
        reserve_seconds = min(seconds / 2, max(60 * reserve['minimum_minutes'],
            min(60 * reserve['maximum_minutes'], seconds * reserve['fraction'])))
    return {'requirement_count': len(ids), 'requirement_ids': ids,
            'requirement_sha256': pack['requirement_sha256'], 'time': timer,
            'total_seconds': seconds, 'delivery_reserve_seconds': reserve_seconds,
            'max_business_repair_rounds': policy['limits']['max_business_repair_rounds']}

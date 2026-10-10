"""Competition preference overlay; base instructions do not depend on this file."""
import os
from pathlib import Path

AGENT_ROOT = Path(__file__).resolve().parents[1]


def profile_enabled():
    name = os.environ.get('HAIKNOW_PROFILE','arc-bench').strip()
    if name == 'none':
        return False
    if name != 'arc-bench':
        raise ValueError('Unknown HAIKNOW_PROFILE: ' + name)
    return True


def profile_policy():
    import json
    return json.loads((AGENT_ROOT/'competition/policy.json').read_text()) if profile_enabled() else {}


def profile_instructions():
    if not profile_enabled():
        return ''
    return (AGENT_ROOT/'competition/instructions.md').read_text(encoding='utf-8')

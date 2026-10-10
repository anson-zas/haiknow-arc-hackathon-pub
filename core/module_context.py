"""Small, frozen module views over the same source-owned acceptance inventory."""
from copy import deepcopy
from hashlib import sha256
import json
import re
from pathlib import Path

from core.requirements import EXTERNAL_DEPENDENCY_HANDLING
from core.scenario_delta import scenario_index
from core.ui_names import requirement_texts


def module_context_path(module_id):
    # YAML IDs are opaque identifiers, never filesystem paths.
    name = sha256(str(module_id).encode()).hexdigest()[:16] + '.json'
    return '.haiknow/modules/' + name


def evolution_index(acceptance, previous, scope):
    """Source comparisons only: neither unchanged prose nor missing history proves implementation."""
    def source(value, req_id):
        row = value.get('requirements', {}).get(req_id)
        if row is None:
            return None
        return {'fields': row['fields'], 'ancestors': row.get('ancestors', []),
                'cases': {cid: {k: v for k, v in case.items() if k != 'module_id'}
                          for cid, case in value.get('cases', {}).items() if case['req_id'] == req_id}}
    policy = json.loads((Path(__file__).resolve().parents[1] / 'competition/evolution.json').read_text())
    pattern = re.compile(policy['description_heading_pattern'], re.I | re.M)
    rows = {}
    for req_id in acceptance['requirements']:
        if req_id not in scope:
            continue
        prior = source(previous, req_id)
        status = 'no_prior_source' if prior is None else (
            'unchanged' if prior == source(acceptance, req_id) else 'changed')
        description = acceptance['requirements'][req_id]['fields'].get('description', '')
        matches = list(pattern.finditer(description)) if isinstance(description, str) else []
        sections = []
        if [m.group(1).lower() for m in matches] == ['original', 'modified']:
            sections = [{'kind': m.group(1).lower(), 'start': m.end(),
                         'end': matches[i+1].start() if i+1 < len(matches) else len(description)}
                        for i, m in enumerate(matches)]
        inherited = []
        for ancestor in acceptance['requirements'][req_id].get('ancestors', []):
            current_fields = acceptance['requirements'][ancestor]['fields']
            previous_fields = previous.get('requirements', {}).get(ancestor, {}).get('fields')
            if current_fields != previous_fields:
                inherited.append({'req_id': ancestor, 'current_fields': deepcopy(current_fields),
                                  'previous_fields': deepcopy(previous_fields)})
        rows[req_id] = {'source_status': status, 'description_sections': sections,
                       'inherited_source_changes': inherited}
    scenarios = scenario_index(acceptance, previous)
    cases = {cid: row for cid, row in scenarios['cases'].items() if row['req_id'] in scope}
    return {'scope': 'Own source fields, ancestor IDs and scenarios; not implementation or inherited behavior coverage',
            'requirements': rows, 'cases': cases,
            'required_case_ids': [cid for cid in scenarios['required_case_ids'] if cid in cases]}


def build_module_context(module, acceptance, *, manual_contract_checks=False, previous=None):
    if manual_contract_checks is not False:
        raise ValueError('Manual contract bindings are not supported')
    requirements = acceptance['requirements']
    if module.node_id not in acceptance['module_order']:
        raise ValueError('Unknown ROOT module: ' + module.node_id)
    own = {rid for rid, row in requirements.items() if row['module_id'] == module.node_id}
    leaves = own.intersection(acceptance['leaf_order'])
    pending = list(leaves)
    while pending:
        for dependency in acceptance['dependencies'][pending.pop()]:
            if dependency not in acceptance['leaf_order']:
                raise ValueError('Unknown dependency leaf: ' + dependency)
            if dependency not in leaves:
                leaves.add(dependency)
                pending.append(dependency)
    scope = own | leaves
    for rid in list(scope):
        scope.update(requirements[rid]['ancestors'])
    state = {key: [row for row in value if row['req_id'] in scope] if isinstance(value, list) else value
             for key, value in acceptance['initial_state'].items()}
    payload = {
        'schema': 'module-source-context/v1', 'module_id': module.node_id,
        'manual_contract_checks': False,
        'module_leaf_ids': [rid for rid in acceptance['leaf_order'] if rid in own],
        'leaf_ids': [rid for rid in acceptance['leaf_order'] if rid in leaves],
        'requirements': {rid: row for rid, row in requirements.items() if rid in scope},
        'cases': {cid: row for cid, row in acceptance['cases'].items() if row['req_id'] in scope},
        'dependencies': {rid: acceptance['dependencies'][rid] for rid in acceptance['leaf_order'] if rid in leaves},
        'module_order': acceptance['module_order'], 'warnings': acceptance['warnings'],
        'references': [row for row in acceptance['references'] if row['req_id'] in scope],
        'initial_state': state,
        'evolution': evolution_index(acceptance, previous or {}, scope),
        'source_texts': [row for row in requirement_texts(acceptance) if row['req_id'] in scope],
        'full_inputs': {'acceptance': '.haiknow/acceptance.json', 'original_yaml_location': '.haiknow/contract.md',
                        'visual_facts': '.haiknow/visual-text.json'},
        'validation_scope': 'Requirement source index only; implement and test the original scenarios. No manual binding gate.',
    }
    external = {rid: refs for rid, refs in acceptance.get('external_dependencies', {}).items()
                if rid in leaves}
    if external:
        payload['external_dependencies'] = external
        payload['external_dependency_handling'] = EXTERNAL_DEPENDENCY_HANDLING
    return deepcopy(payload)


def write_module_contexts(knowledge_dir, modules, acceptance, *, manual_contract_checks=False):
    """Return generated files and hashes for the controller's frozen-input guard."""
    if manual_contract_checks is not False:
        raise ValueError('Manual contract bindings are not supported')
    knowledge_dir = Path(knowledge_dir)
    folder = knowledge_dir / 'modules'
    folder.mkdir(parents=True, exist_ok=True)
    frozen, index = {}, {}
    previous_path = knowledge_dir / 'prior-acceptance.json'
    previous = json.loads(previous_path.read_text()) if previous_path.is_file() else {}
    for module in modules:
        relative = module_context_path(module.node_id)
        path = folder / Path(relative).name
        payload = build_module_context(module, acceptance, previous=previous)
        path.write_text(json.dumps(payload, ensure_ascii=False, separators=(',', ':')) + '\n', encoding='utf-8')
        frozen[path] = sha256(path.read_bytes()).hexdigest()
        index[module.node_id] = {'path': relative, 'sha256': frozen[path],
                                  'module_leaf_ids': payload['module_leaf_ids']}
    path = folder / 'index.json'
    path.write_text(json.dumps({'schema': 'module-source-index/v1', 'modules': index}, ensure_ascii=False,
                               separators=(',', ':')) + '\n', encoding='utf-8')
    frozen[path] = sha256(path.read_bytes()).hexdigest()
    return frozen

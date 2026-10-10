"""Lossless YAML index and stable code-only dependency ordering; no model I/O."""
from copy import deepcopy
import re
from core.initial_state import compile_initial_state


EXTERNAL_DEPENDENCY_HANDLING = (
    'External dependency IDs are unverified prerequisites outside this input, not completed requirements. '
    'Inspect and test existing application capabilities; when absent, implement only the prerequisite '
    'behavior supported by the current descriptions and scenarios. Do not invent source requirements '
    'or passed tests.'
)


def stable_order(ids, dependencies):
    pending = list(ids)
    done = set()
    result = []
    while pending:
        ready = next((key for key in pending if dependencies[key] <= done), None)
        if ready is None:
            raise ValueError('Cyclic requirement dependencies: ' + ', '.join(pending))
        pending.remove(ready)
        done.add(ready)
        result.append(ready)
    return result


def compile_acceptance(tree, *, allow_external_dependencies=False):
    """Preserve fields/scenarios; infer only index ownership and execution edges.

    Dependencies on an ancestor express enclosing context, not completion of all
    its descendants. Other folder dependencies expand to their actual leaves.
    A cyclic module projection can have an acyclic leaf graph: preserve module
    order in that case, and expose pending cross-module dependencies explicitly.
    The original single-thread whole-module architecture is not split into jobs.
    Delta inputs may explicitly retain out-of-tree references as unverified
    prerequisites; they never become synthetic requirements or completed work.
    """
    requirements = {}
    cases = {}
    raw = {}
    leaves = []
    references = []

    def visit(node, ancestors, module):
        key = str(node.get('id') or node.get('req_id') or '').strip()
        if not key or key in raw:
            raise ValueError('Missing or duplicate requirement id: ' + key)
        raw[key] = node
        requirements[key] = {
            'ancestors': list(ancestors), 'module_id': module,
            'fields': deepcopy({k:v for k,v in node.items() if k not in {'children','scenarios'}}),
        }
        scenarios = node.get('scenarios') or []
        if not isinstance(scenarios, list) or not all(isinstance(s, dict) for s in scenarios):
            raise ValueError('Invalid scenarios: ' + key)
        for i, scenario in enumerate(scenarios, 1):
            cases[f'{key}:S{i}'] = {'source':'scenario','req_id':key,'module_id':module,
                                    'scenario':deepcopy(scenario)}
        for path in re.findall(r'!\[[^\]]*\]\(([^)]+)\)', str(node.get('description') or '')):
            references.append({'req_id':key,'path':path})
        children = node.get('children') or []
        if not children:
            leaves.append(key)
            if not scenarios:
                cases[f'{key}:D1'] = {'source':'description','req_id':key,'module_id':module,
                                      'description':deepcopy(node.get('description',''))}
        for child in children:
            child_id = str(child.get('id') or child.get('req_id') or '').strip()
            visit(child, [*ancestors,key], child_id if key == 'ROOT' else module)

    visit(tree, [], None)
    descendants = {key:{leaf for leaf in leaves if leaf == key or key in requirements[leaf]['ancestors']}
                   for key in raw}
    dependencies = {}
    external_dependencies = {}
    for leaf in leaves:
        parents = requirements[leaf]['ancestors']
        needed = set()
        external = set()
        for owner in [*parents,leaf]:
            refs = raw[owner].get('dependencies') or []
            if not isinstance(refs, list) or not all(isinstance(r, str) for r in refs):
                raise ValueError('Invalid dependencies: ' + owner)
            for ref in refs:
                if ref not in raw:
                    if not allow_external_dependencies:
                        raise ValueError('Unknown dependency: ' + ref)
                    external.add(ref)
                    continue
                if ref in parents:
                    continue
                needed.update(descendants[ref])
        dependencies[leaf] = needed
        if external:
            external_dependencies[leaf] = sorted(external)
    leaf_order = stable_order(leaves, dependencies)
    module_ids = [str(c.get('id') or c.get('req_id')).strip() for c in tree['children']]
    module_deps = {key:set() for key in module_ids}
    for leaf, refs in dependencies.items():
        owner = requirements[leaf]['module_id']
        module_deps[owner].update(requirements[r]['module_id'] for r in refs
                                  if requirements[r]['module_id'] != owner)
    warnings = []
    if external_dependencies:
        warnings.append(EXTERNAL_DEPENDENCY_HANDLING + ' Referenced IDs: ' + ', '.join(
            sorted({ref for refs in external_dependencies.values() for ref in refs})))
    try:
        module_order = stable_order(module_ids, module_deps)
    except ValueError:
        module_order = module_ids
        warnings.append('Interleaved module dependencies: implement shared prerequisites as needed; '
                        'revisit pending cross-module journeys during final integration.')
    consumers = {key:[] for key in leaves}
    for leaf, refs in dependencies.items():
        for ref in refs:
            consumers[ref].append(leaf)
    initial_state = compile_initial_state(requirements, cases)
    from core.runtime_contracts import compile_runtime_contracts
    runtime_contracts = compile_runtime_contracts({'requirements':requirements,'cases':cases})
    compiled = {'runtime_contracts':runtime_contracts, 'initial_state': initial_state, 'initial_records': [f for f in initial_state['facts'] if f['predicate']=='exists'], 'schema_version':1,'requirements':requirements,'cases':cases,'references':references,
            'leaf_order':leaf_order,'dependencies':{k:sorted(v) for k,v in dependencies.items()},
            'module_order':module_order,'module_dependencies':{k:sorted(v) for k,v in module_deps.items()},
            'shared_prerequisites':[{'req_id':k,'consumers':consumers[k]} for k in leaves if len(consumers[k])>1],
            'warnings':warnings}
    if external_dependencies:
        compiled['external_dependencies'] = external_dependencies
    return compiled


def module_index(compiled, module_id):
    relevant = {key for key, value in compiled['requirements'].items() if value['module_id'] == module_id}
    external = {
        key: sorted({ref for ref in refs if ref not in relevant}
                    | set(compiled.get('external_dependencies', {}).get(key, [])))
        for key, refs in compiled['dependencies'].items() if key in relevant
    }
    state = compiled['initial_state']
    def ids(key):
        return [f['id'] for f in state.get(key, []) if f['req_id'] in relevant or f['req_id'] == 'ROOT']
    # The same declaration often repeats in all scenario steps. Keep the text
    # once in the actual coding prompt, without dropping distinct owner/role data.
    seeds = {}
    for row in state.get('scenario_seeds', []):
        if row['req_id'] not in relevant and row['req_id'] != 'ROOT':
            continue
        entry = seeds.setdefault(row['source_quote'], {'text':row['source_quote'], 'req_ids':[], 'case_ids':[]})
        for key, value in [('req_ids', row['req_id']), ('case_ids', row['case_id'])]:
            if value not in entry[key]:
                entry[key].append(value)
    result = {'initial_state': {'fact_ids': ids('facts'), 'constraint_ids': ids('constraints'),
                              'scenario_precondition_ids': ids('scenario_preconditions'),
                              'scenario_seed_ids': ids('scenario_seeds'),
                              'unverified_source_ids': ids('unverified')},
            'seed_contracts': list(seeds.values()),
            'runtime_contract_ids':[c['id'] for c in compiled.get('runtime_contracts',[]) if c['req_id'] in relevant or c['req_id']=='ROOT'],
            'acceptance_source':'.haiknow/acceptance.json',
            'case_ids':[key for key,value in compiled['cases'].items()
                        if value['req_id'] in relevant or value['req_id']=='ROOT'],
            'leaf_order':[key for key in compiled['leaf_order'] if key in relevant],
            'external_dependencies':{key: refs for key, refs in external.items() if refs},
            'warnings':compiled['warnings']}
    if any(key in relevant for key in compiled.get('external_dependencies', {})):
        result['external_dependency_handling'] = EXTERNAL_DEPENDENCY_HANDLING
    return result

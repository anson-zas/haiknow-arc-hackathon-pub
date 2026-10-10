"""Byte-addressed requirement inventory; semantic interpretation remains explicit.

Every scalar other than structural ids/dependencies is retained, including unknown
fields. Raw YAML slices and decoded values are deliberately separate: folded and
quoted scalars do not have a one-to-one decoded-character/raw-byte mapping.
"""
from hashlib import sha256
import yaml
from yaml.nodes import MappingNode, ScalarNode, SequenceNode
from core.requirements import EXTERNAL_DEPENDENCY_HANDLING, stable_order


def compile_source_pack(raw: bytes, *, allow_external_dependencies=False):
    text = raw.decode('utf-8')
    if any(isinstance(event, yaml.events.AliasEvent) for event in yaml.parse(text)):
        raise ValueError('YAML aliases are not supported: preserve unambiguous source ownership')
    root = yaml.compose(text)
    nodes = {}

    def index(node, path):
        nodes[path] = node
        if isinstance(node, MappingNode):
            seen = set()
            for key, value in node.value:
                if not isinstance(key, ScalarNode) or key.value in seen:
                    raise ValueError('Duplicate or non-scalar YAML key at ' + path)
                seen.add(key.value)
                index(value, path + '/' + key.value.replace('~', '~0').replace('/', '~1'))
        elif isinstance(node, SequenceNode):
            for i, value in enumerate(node.value):
                index(value, path + '/' + str(i))
    if not isinstance(root, MappingNode):
        raise ValueError('Requirement root must be a mapping')
    index(root, '')
    tree = yaml.safe_load(text)
    sources, scenarios, requirements = [], [], {}

    def scalar(path, req_id, scenario_id, field):
        node = nodes[path]
        if isinstance(node, ScalarNode):
            start, end = node.start_mark.index, node.end_mark.index
            source_id = 'SRC-' + sha256(path.encode()).hexdigest()[:16]
            sources.append({'id': source_id, 'req_id': req_id, 'scenario_id': scenario_id,
                'field': field, 'path': path, 'value': node.value, 'raw': text[start:end],
                'byte_span': [len(text[:start].encode()), len(text[:end].encode())],
                'start': [node.start_mark.line + 1, node.start_mark.column + 1],
                'end': [node.end_mark.line + 1, node.end_mark.column + 1],
                'semantic_status': 'unreviewed'})
        else:
            prefix = path + '/'
            # Only direct children; recursive traversal keeps each scalar once.
            for child in [p for p in nodes if p.startswith(prefix) and '/' not in p[len(prefix):]]:
                scalar(child, req_id, scenario_id, field)

    def visit(node, path, ancestors):
        if not isinstance(node, dict):
            raise ValueError('Requirement must be a mapping')
        req_id = node.get('id', node.get('req_id'))
        if not isinstance(req_id, str) or not req_id or req_id in requirements:
            raise ValueError('Missing or duplicate requirement id')
        children = node.get('children', [])
        if children is None: children = []
        scenario_list = node.get('scenarios', [])
        if scenario_list is None: scenario_list = []
        if not isinstance(children, list) or not isinstance(scenario_list, list):
            raise ValueError('children/scenarios must be lists')
        requirements[req_id] = {'ancestors': ancestors, 'dependencies': node.get('dependencies') or [],
                                'leaf': not children}
        for field in node:
            if field not in {'id', 'req_id', 'children', 'scenarios', 'dependencies'}:
                scalar(path + '/' + field.replace('~', '~0').replace('/', '~1'), req_id, None, field)
        for i, scenario in enumerate(scenario_list):
            sid = f'{req_id}:S{i + 1}'
            if not isinstance(scenario, dict):
                raise ValueError('Scenario must be a mapping')
            for field in scenario:
                scalar(f'{path}/scenarios/{i}/' + field.replace('~', '~0').replace('/', '~1'), req_id, sid, field)
            scenarios.append({'id': sid, 'req_id': req_id})
        if not children and not scenario_list:
            scenarios.append({'id': f'{req_id}:D1', 'req_id': req_id})
        for i, child in enumerate(children):
            visit(child, f'{path}/children/{i}', [*ancestors, req_id])
    visit(tree, '', [])
    # Keep byte addresses/field intact; annotate canonical nested step semantics.
    for source in sources:
        if source['scenario_id'] and source['field'] == 'steps':
            path = source['path']
            if path.endswith('/keyword'):
                source['step_role'] = 'keyword'
            elif path.endswith('/content'):
                keyword = nodes.get(path.rsplit('/', 1)[0] + '/keyword')
                if isinstance(keyword, ScalarNode):
                    source['step_role'] = keyword.value.strip().lower()
    for req in requirements.values():
        if not isinstance(req['dependencies'], list) or any(not isinstance(d, str) for d in req['dependencies']):
            raise ValueError('Invalid requirement dependencies')
        if not allow_external_dependencies and any(d not in requirements for d in req['dependencies']):
            raise ValueError('Unknown requirement dependency')
    # External references remain source-owned prerequisites, not invented leaves.
    # Match acceptance ordering: ancestor references are enclosing context; other
    # internal folder references expand to leaves before checking for cycles.
    leaves = [rid for rid, req in requirements.items() if req['leaf']]
    descendants = {rid: {leaf for leaf in leaves
                        if leaf == rid or rid in requirements[leaf]['ancestors']}
                   for rid in requirements}
    internal, external = {}, {}
    for leaf in leaves:
        ancestors = requirements[leaf]['ancestors']
        needed, missing = set(), set()
        for owner in [*ancestors, leaf]:
            for ref in requirements[owner]['dependencies']:
                if ref not in requirements:
                    missing.add(ref)
                elif ref not in ancestors:
                    needed.update(descendants[ref])
        internal[leaf] = needed
        if missing:
            external[leaf] = sorted(missing)
    stable_order(leaves, internal)
    for scenario in scenarios:
        owners = {scenario['req_id'], *requirements[scenario['req_id']]['ancestors']}
        scenario['source_refs'] = [s['id'] for s in sources if s['scenario_id'] == scenario['id'] or
                                  (s['scenario_id'] is None and s['req_id'] in owners)]
    result = {'schema': 'requirement-sources/v1', 'requirement_sha256': sha256(raw).hexdigest(),
            'sources': sources, 'scenarios': scenarios, 'requirements': requirements,
            'coverage_note': 'Source inventory only; semantic obligation coverage requires independent review.'}
    if external:
        result['external_dependencies'] = external
        result['warnings'] = [EXTERNAL_DEPENDENCY_HANDLING]
    return result

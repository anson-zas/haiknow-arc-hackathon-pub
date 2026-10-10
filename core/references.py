"""Bind necessary image text to original requirements, never to a whole OCR dump."""
from copy import deepcopy
from hashlib import sha256
import json
import os
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit
from core.profile import profile_enabled

POLICY = Path(__file__).resolve().parents[1] / 'competition/reference-images.json'


def images_enabled():
    if not profile_enabled():
        return False
    default = json.loads(POLICY.read_text())['enabled']
    switch = os.environ.get('HAIKNOW_PARSE_REFERENCE_IMAGES', str(default)).strip().lower()
    if switch not in {'true', 'false'}:
        raise ValueError('HAIKNOW_PARSE_REFERENCE_IMAGES must be true or false')
    return switch == 'true'


def reference_contexts(requirements_dir, acceptance):
    """Only explicit functional delegations qualify. Unknown wording stays unresolved.

    Matching patterns are configurable because future organizer wording is unknown.
    A mention of an image, style or layout alone never enables extraction.
    """
    if not profile_enabled():
        return {}
    root = Path(requirements_dir).resolve()
    policy = json.loads(POLICY.read_text())
    result = {}
    nodes = acceptance['requirements']
    for req_id, node in nodes.items():
        scenarios = [v['scenario'] for v in acceptance['cases'].values()
                     if v['req_id'] == req_id and v['source'] == 'scenario']
        description = str(node['fields'].get('description', ''))
        texts = [description]
        for scenario in scenarios:
            texts.extend(str(step.get('content', '')) for step in scenario.get('steps', []))
        requests = []
        for item in policy['delegation_patterns']:
            for text in texts:
                requests.extend({'kind': item['kind'], 'source_quote': match.group(0)}
                                for match in re.finditer(item['pattern'], text, flags=re.I))
        if not requests:
            continue
        owner_ids = {req_id, *node['ancestors']}
        for reference in acceptance['references']:
            if reference['req_id'] not in owner_ids:
                continue
            url = urlsplit(reference['path'])
            if url.scheme or url.netloc:
                continue
            candidate = root / unquote(url.path)
            path = candidate.resolve()
            if not path.is_relative_to(root) or candidate.is_symlink() or not path.is_file():
                continue
            if path.stat().st_size > 20 * 1024 * 1024:
                continue
            digest = sha256(path.read_bytes()).hexdigest()
            result.setdefault(digest, []).append({
                'req_id': req_id, 'image_path': str(path.relative_to(root)),
                'description': description, 'scenarios': deepcopy(scenarios),
                'ancestors': [{'req_id': key, 'fields': deepcopy(nodes[key]['fields'])}
                              for key in node['ancestors']],
                'requests': requests,
            })
    return result


def parse_visual_facts(note, contexts):
    payload = json.loads(note)
    if not isinstance(payload, dict) or not isinstance(payload.get('facts'), list):
        raise ValueError('Expected scoped visual facts JSON')
    if type(payload.get('required_by_text')) is not bool:
        raise ValueError('Missing required_by_text decision')
    if not payload['required_by_text'] and payload['facts']:
        raise ValueError('Style-only references cannot add facts')
    if len(payload['facts']) > 16:
        raise ValueError('Too many visual facts; not a full-image OCR channel')
    facts = []
    for fact in payload['facts']:
        if not isinstance(fact, dict) or not isinstance(fact.get('text'), str) or not 0 < len(fact['text']) <= 1000:
            raise ValueError('Invalid exact visual text')
        owners = [context for context in contexts if context['req_id'] == fact.get('req_id')
                  and any(request['kind'] == fact.get('kind') and
                          request['source_quote'] == fact.get('source_quote') for request in context['requests'])]
        if not owners:
            raise ValueError('Visual fact has no matching original functional delegation')
        facts.append({key: fact[key] for key in ('req_id', 'kind', 'source_quote', 'text')} |
                     {'image_path': owners[0]['image_path'], 'source': 'scoped_visual_extraction'})
    return facts

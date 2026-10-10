"""Conservative source-scenario identity; no model, ordinal matching or test claims."""
from collections import defaultdict
from copy import deepcopy
import json


def _fingerprint(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _name(scenario):
    value = scenario.get('name')
    return value if isinstance(value, str) and value.strip() else None


def _unique_matches(current, previous, key):
    def group(cases):
        values = defaultdict(list)
        for case_id, case in cases.items():
            value = key(case['scenario'])
            if value is not None:
                values[value].append(case_id)
        return values
    before, after = group(previous), group(current)
    return {ids[0]: before[value][0] for value, ids in after.items()
            if len(ids) == 1 and len(before.get(value, [])) == 1}


def _steps(current, previous):
    after = current.get('steps', [])
    before = previous.get('steps', []) if previous is not None else None
    # This is a positional source diff, never an inferred identity for a moved step.
    changed = None if before is None else [i for i in range(max(len(after), len(before)))
        if i >= len(after) or i >= len(before) or _fingerprint(after[i]) != _fingerprint(before[i])]
    return {'current': deepcopy(after), 'previous': deepcopy(before), 'changed_indices': changed}


def scenario_index(acceptance, previous):
    """Only uniquely matched complete scenarios can be exempted from execution checks.

    Current case IDs remain source locations. Match within one requirement by
    complete content first, then by a unique remaining name on both sides. With
    unmatched old scenarios left, a new-looking case could be a rename: report
    ambiguity instead of assigning identity from an ordinal or similarity.
    """
    previous = previous or {}
    current = {cid: row for cid, row in acceptance.get('cases', {}).items()
               if row.get('source') == 'scenario'}
    prior = {cid: row for cid, row in previous.get('cases', {}).items()
             if row.get('source') == 'scenario'}
    identities = {}
    for req_id in dict.fromkeys(row['req_id'] for row in current.values()):
        after = {cid: row for cid, row in current.items() if row['req_id'] == req_id}
        before = {cid: row for cid, row in prior.items() if row['req_id'] == req_id}
        if req_id not in previous.get('requirements', {}):
            identities.update({cid: ('no_prior_source', None) for cid in after})
            continue
        for status, key in [('unchanged', _fingerprint), ('changed', _name)]:
            for cid, old_id in _unique_matches(after, before, key).items():
                identities[cid] = (status, old_id)
                del after[cid]
                del before[old_id]
        identities.update({cid: ('ambiguous' if before else 'new', None) for cid in after})
    rows = {}
    for cid, case in current.items():
        status, old_id = identities[cid]
        rows[cid] = {'req_id': case['req_id'], 'source_status': status,
                     'previous_case_id': old_id,
                     'steps': _steps(case['scenario'], prior[old_id]['scenario'] if old_id is not None else None)}
    return {'cases': rows,
            'required_case_ids': [cid for cid, row in rows.items() if row['source_status'] != 'unchanged'],
            'scope': 'Source-scenario identity only; step indices compare source positions, not moved-step identity. '
                     'Descriptions and inherited constraints remain context, not scenario execution coverage.'}

"""Check explicit source scopes against statically resolved test actions."""
import json
from pathlib import Path
import re


def _source_scopes(acceptance):
    policy = json.loads((Path(__file__).parents[1]/'competition/locator-scope.json').read_text())
    suffix = re.compile(policy['main_suffix_pattern'], re.I)
    boundary = re.compile(policy['sentence_boundary_pattern'])
    uncertain = re.compile(policy['uncertain_clause_pattern'], re.I)
    click_prefix = re.compile(policy['click_prefix_pattern'], re.I)
    locate_prefix = re.compile(policy['locate_prefix_pattern'], re.I)
    locate_click_suffix = re.compile(policy['locate_click_suffix_pattern'], re.I)
    click_uncertain = re.compile(policy['click_uncertain_clause_pattern'], re.I)
    patterns = []
    for opening, closing in policy['quote_pairs']:
        quoted = re.escape(opening) + '(?P<name>[^' + re.escape(closing) + '\r\n]+)' + re.escape(closing)
        patterns.extend(re.compile(pattern.replace('{quoted}', quoted), re.I)
                        for pattern in policy['role_name_patterns'])
    groups = {}
    for case_id, case in acceptance.get('cases', {}).items():
        if case.get('source') != 'scenario':
            continue
        for index, step in enumerate(case.get('scenario', {}).get('steps', [])):
            text = step.get('content')
            if not isinstance(text, str):
                continue
            for pattern in patterns:
                for match in pattern.finditer(text):
                    role, name = match['role'].lower(), match['name']
                    # Only adjacent scope syntax is recognized. Context may veto
                    # an obligation, but never supplies a scope from another clause.
                    before = list(boundary.finditer(text, 0, match.start()))
                    after = boundary.search(text, match.end())
                    start, end = before[-1].end() if before else 0, after.start() if after else len(text)
                    context = text[start:match.start('name')] + text[match.end('name'):end]
                    main = suffix.match(text[match.end():])
                    scope = 'main' if main and not uncertain.search(context) else None
                    # A scoped noun is not a click obligation. Only literal WHEN
                    # steps and adjacent, positive actions can require evidence.
                    prefix = text[start:match.start()]
                    requires_click = bool(scope and step.get('keyword') == 'WHEN'
                        and not click_uncertain.search(context)
                        and (click_prefix.search(prefix) or (locate_prefix.search(prefix)
                            and locate_click_suffix.match(text[match.end()+main.end():]))))
                    row = {'source_path': f'{case_id}.steps.{index}', 'source_quote': text,
                           'name': name, 'scope': scope, 'requires_click_evidence': requires_click}
                    rows = groups.setdefault((case_id, role, name.casefold()), [])
                    if row not in rows:
                        rows.append(row)
    return groups


def check_locator_scope(acceptance, observations):
    result = {'issues': [], 'unverified': [], 'scope':
              'Explicit quoted source main scopes versus attributed test clicks; '
              'this checks test locator scope, not application correctness or complete journey semantics.'}
    observations = [*observations.get('controls', []), *observations.get('unresolved', [])]
    for (case_id, role, folded_name), sources in _source_scopes(acceptance).items():
        required = [row for row in sources if row['scope'] == 'main']
        if not required:
            continue
        original = next((row for row in required if row['requires_click_evidence']), required[0])
        base = {'source_case_id': case_id, 'role': role, 'name': original['name'],
                'required_scope': 'main', 'source_path': original['source_path'],
                'source_quote': original['source_quote'],
                'requires_click_evidence': len(required) == len(sources)
                    and any(row['requires_click_evidence'] for row in required),
                'sources': [{key: row[key] for key in ('source_path', 'source_quote')} for row in required]}
        candidates = [row for row in observations if row.get('source') == 'test-locator'
                      and row.get('source_case_id') == case_id and row.get('role') == role
                      and row.get('locator_action') in {'click', 'unknown'}
                      and (not isinstance(row.get('name'), str) or row['name'].casefold() == folded_name)]
        reason = None
        if len(required) != len(sources):
            reason = 'source_scope_ambiguous'
        elif not candidates:
            reason = 'source_scope_has_no_attributed_click'
        elif any(not isinstance(row.get('name'), str) or row.get('locator_action') != 'click'
                 or row.get('locator_scope') not in {'main', 'page'} for row in candidates):
            reason = 'test_locator_scope_unresolved'
        elif len({row['locator_scope'] for row in candidates}) > 1:
            reason = 'test_locator_scope_ambiguous'
        if reason:
            result['unverified'].append({**base, 'reason': reason})
        elif all(row['locator_scope'] == 'page' for row in candidates):
            for row in candidates:
                result['issues'].append({**row, **base, 'reason': 'requirement_test_scope_missing'})
    return result

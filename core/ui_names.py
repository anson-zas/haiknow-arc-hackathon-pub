"""Code-owned comparison with the frozen YAML; no model or grader data."""
from difflib import SequenceMatcher
import re
from core.profile import profile_policy


def load_name_acceptance(project):
    """Retained features keep their sources; current input owns changed IDs.

    This is only a naming/role view. It never adds previous cases or completion
    state to the current execution inventory, nor exempts old controls from checks.
    """
    import json
    from core.workspace import merge_requirement_sources
    current = json.loads((project / '.haiknow/acceptance.json').read_text())
    prior_path = project / '.haiknow/prior-acceptance.json'
    if prior_path.is_symlink():
        raise ValueError('Prior requirement sources must not be a symlink')
    if not prior_path.exists():
        return current
    prior = json.loads(prior_path.read_text())
    return {**current, **merge_requirement_sources(prior, current)}


def requirement_texts(acceptance):
    result = []

    def visit(value, req_id, path):
        if isinstance(value, str):
            result.append({'req_id': req_id, 'path': path, 'text': value})
        elif isinstance(value, dict):
            for key, child in value.items():
                if key not in {'id', 'type', 'dependencies', 'ancestors', 'module_id'}:
                    visit(child, req_id, path + '.' + key)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, req_id, f'{path}[{index}]')

    for req_id, node in acceptance['requirements'].items():
        visit(node['fields'], req_id, 'fields')
    for case in acceptance['cases'].values():
        if case['source'] == 'scenario':
            visit(case['scenario'], case['req_id'], 'scenario')
    return result


def _contains(text, name):
    # Case only: no synonyms, punctuation removal or whitespace normalization.
    # ASCII word edges prevent Archive from being accepted solely by Archived.
    folded = name.casefold()
    left = r'(?<![a-z0-9_])' if folded[:1].isascii() and folded[:1].isalnum() else ''
    right = r'(?![a-z0-9_])' if folded[-1:].isascii() and folded[-1:].isalnum() else ''
    return bool(folded) and re.search(left + re.escape(folded) + right, text.casefold()) is not None


def scoped_texts(acceptance, texts, req_id):
    if not req_id:
        return texts
    owners = {req_id, *acceptance['requirements'].get(req_id, {}).get('ancestors', [])}
    return [item for item in texts if item['req_id'] in owners]


def check_names(acceptance, observations, reviews=None, visual_facts=None):
    texts = requirement_texts(acceptance)
    texts.extend({'req_id': fact['req_id'], 'path': 'visual:' + fact['image_path'], 'text': fact['text']}
                 for fact in (visual_facts or []) if fact['req_id'] in acceptance['requirements'])
    matched, issues, explained = [], [], []
    unresolved = list(observations.get('unresolved', []))
    policy = profile_policy()
    fixed_controls = {(item.get('role'), item['name'].casefold())
                      for item in observations['controls'] if item.get('source') == 'jsx-static'}
    quoted = [name for item in texts for name in re.findall(r'["“`]([^"”`\n]+)["”`]', item['text'])]
    # A quoted seed/record name is not necessarily a fixed product control.
    # Reuse the existing source-backed control-role patterns; do not guess
    # whether arbitrary prose describes editable data.
    fixed_names = [name for name in quoted if any(
        re.search(policy[key].replace('{name}', re.escape(name)), item['text'], re.I)
        for item in texts for key in ('explicit_link_pattern', 'explicit_action_pattern') if policy.get(key))]

    def altered_explicit_name(name, originals=quoted):
        folded = name.casefold()
        compact = ''.join(c for c in folded if c.isalnum())
        return any(compact == ''.join(c for c in original.casefold() if c.isalnum())
                   or folded.startswith(original.casefold() + ' ')
                   for original in originals if original)
    for control in observations['controls']:
        name = control['name']
        applicable = scoped_texts(acceptance, texts, control.get('req_id'))
        sources = [item for item in applicable if _contains(item['text'], name)]
        if sources:
            matched.append({**control, 'requirements': [
                {'req_id': item['req_id'], 'path': item['path']} for item in sources]})
            if control.get('source') == 'test-locator' and not control.get('exact'):
                issues.append({**control, 'reason': 'non_exact_test_locator', 'context': sources[:3]})
        else:
            record_locator = (control.get('source') == 'test-locator'
                    and (control.get('role') in {'link', 'article'}
                         or control.get('role') == 'button' and control.get('record_scope'))
                    and not control.get('navigation_scope')
                    and (control.get('role'), name.casefold()) not in fixed_controls)
            input_proven = any(item.get('kind') == 'test-input' for item in control.get('fixture_sources', []))
            fixed_drift = altered_explicit_name(name, fixed_names)
            if (record_locator and control.get('fixture_sources')
                    and policy.get('local_mock_names_are_data')
                    and (not altered_explicit_name(name) or input_proven and not fixed_drift)):
                explained.append({**control, 'status': 'test_fixture_data'})
                continue
            if (record_locator and control.get('name_from_constant')
                    and not control.get('fixture_sources') and not fixed_drift):
                pending = {**control, 'candidate_name': name, 'reason': 'dynamic_record_name_needs_provenance'}
                pending.pop('name')
                unresolved.append(pending)
                continue
            review = next((item for item in (reviews or []) if item.get('file') == control.get('file')
                           and item.get('name') == name), None)
            if review and not altered_explicit_name(name) and review.get('kind') == 'requirement_unnamed' and str(review.get('reason', '')).strip():
                quote = review.get('source_quote', '')
                referenced = [item for item in applicable if item['req_id'] == review.get('req_id')
                              and quote and quote in item['text']]
                if referenced:
                    explained.append({**control, 'review': review,
                                      'status': 'explained_unverified'})
                    continue
            closest = sorted(texts, key=lambda item: SequenceMatcher(
                None, name.casefold(), item['text'].casefold()).ratio(), reverse=True)[:3]
            issues.append({**control, 'reason': 'name_not_in_requirements',
                           'context': closest})
    return {'matched': matched, 'issues': issues, 'explained_unverified': explained,
            'unresolved': unresolved,
            'scope': 'statically resolved controls and test locators; dynamic/unvisited UI is not certified'}


def failure_message(audit):
    lines = ['UI names do not match the frozen requirements (case-insensitive). '
             'Fix explicitly named UI and requirement-derived tests; do not weaken locators or edit the frozen input. '
             'For a control the requirement does not name, record file/name/kind=requirement_unnamed/'
             'req_id/source_quote/reason in .haiknow/ui-name-review.json (JSON array). '
             'The quote must be copied verbatim from that REQ. An explanation is not a verified name match.']
    for item in audit['issues'][:20]:
        refs = ', '.join(dict.fromkeys(row['req_id'] for row in item.get('context', [])))
        lines.append(f"{item.get('file', '?')}:{item.get('line', '?')} {item.get('role', '')} "
                     f"{item['name']!r}: {item['reason']}; related text to inspect: {refs}")
    return '\n'.join(lines)


def check_semantics(acceptance, observations, *, scope_acceptance=None):
    """Check source-backed button/link intent; ambiguous roles stay unverified."""
    policy = profile_policy()
    result = {'issues': [],
              'unverified': list(observations.get('interaction_unverified', [])),
              'scope': 'static roles; click-handler intent, focus and journeys require browser verification'}
    if not policy.get('actions_use_buttons'):
        return result
    from core.locator_scope import check_locator_scope
    scope_audit = check_locator_scope(acceptance if scope_acceptance is None else scope_acceptance, observations)
    result['issues'].extend(scope_audit['issues'])
    result['unverified'].extend(scope_audit['unverified'])
    # The raw audit remains epistemic: unknown helpers are not app defects.
    # A definite current WHEN click still needs verifiable self-test evidence.
    result['issues'].extend({**item, 'evidence_reason': item['reason'],
                             'reason': 'requirement_test_scope_evidence_missing'}
        for item in scope_audit['unverified'] if item.get('requires_click_evidence')
        and item['reason'] in {'source_scope_has_no_attributed_click',
                              'test_locator_scope_unresolved', 'test_locator_scope_ambiguous'})
    # No routes, bindings or generated DOM may redefine a literal YAML role.
    # Containers may share names with children and outer containers. Flag a
    # substituted role only when the same test file has no locator for the
    # explicit role. This is a static test contract check, not UI coverage proof.
    from core.runtime_contracts import compile_runtime_contracts, runtime_policy
    containers = {'group', 'region', 'form', 'dialog', 'article', 'navigation', 'complementary'}
    contracts = [c for c in compile_runtime_contracts(acceptance) if c['role'] in containers and c.get('name')]
    negative = runtime_policy().get('container_role_negative_pattern')
    locators = [c for c in [*observations.get('controls', []), *observations.get('container_locators', [])]
                if c.get('source') == 'test-locator']
    for control in locators:
        if control.get('role') not in containers or not control.get('name'):
            continue
        owner = control.get('req_id')
        owners = {owner, *acceptance['requirements'].get(owner, {}).get('ancestors', [])} if owner else None
        sources = [c for c in contracts if c['name'].casefold() == control['name'].casefold()
                   and (owners is None or c['req_id'] in owners)]
        if not sources:
            continue
        detail = {**control, 'context': [{'req_id':c['req_id'], 'text':c['source_quote'],
                    'path':c['source_path']} for c in sources]}
        if negative and any(re.search(negative, c['source_quote'], re.I) for c in sources):
            result['unverified'].append({**detail, 'reason':'negated_container_role_unverified'})
            continue
        expected = sorted({c['role'] for c in sources})
        if len(expected) > 1:
            result['unverified'].append({**detail, 'reason':'container_role_conflict_needs_req_scope',
                                         'expected_roles':expected})
        elif control['role'] not in expected and not any(
                other.get('file') == control.get('file') and other.get('name', '').casefold() == control['name'].casefold()
                and other.get('role') in expected for other in locators):
            result['issues'].append({**detail, 'reason':'requirement_explicit_container_role_mismatch',
                                     'expected_roles':expected})
    texts = requirement_texts(acceptance)
    for control in observations.get('controls', []):
        if control.get('role') not in {'link', 'button'} or control.get('source') == 'test-locator':
            continue
        name = re.escape(control['name'])
        applicable = scoped_texts(acceptance, texts, control.get('req_id'))
        alternative = policy.get('alternative_navigation_pattern')
        if alternative:
            # Remove only the matched alternative phrase before testing exclusive
            # roles. Do not let one permissive phrase hide another explicit role.
            applicable = [{**t, 'text': re.sub(alternative.replace('{name}', name), '', t['text'], flags=re.I)}
                          for t in applicable]
        links = [t for t in applicable if re.search(policy['explicit_link_pattern'].replace('{name}', name), t['text'], re.I)]
        actions = [t for t in applicable if re.search(policy['explicit_action_pattern'].replace('{name}', name), t['text'], re.I)]
        if links and policy.get('respect_explicit_link_requirement', True):
            if actions:
                result['unverified'].append({**control, 'reason': 'role_conflict_needs_req_scope', 'context': links + actions})
            elif control['role'] != 'link':
                result['issues'].append({**control, 'reason': 'requirement_explicit_link_requires_link', 'context': links})
            continue
        if control['role'] == 'link' and (actions or (control.get('on_click') and not control.get('has_destination'))):
            result['issues'].append({**control, 'reason': 'functional_action_requires_native_button', 'context': actions})
    return result


def semantic_issue_message(issue):
    """Keep scope evidence actionable in existing checkpoint and repair turns."""
    location = (f"{issue['file']}:{issue.get('line', '?')}" if issue.get('file')
                else issue.get('source_path', '?'))
    message = f"{location} {issue.get('name')!r}: {issue['reason']}"
    if issue.get('expected_roles'):
        message += '; source requires ' + ', '.join(issue['expected_roles'])
    if issue.get('reason') == 'requirement_test_scope_missing':
        message += (f"; case {issue.get('case_id') or issue.get('source_case_id')}; "
                    "the source explicitly requires main scope. Strengthen this test's locator "
                    "to the required main scope, then run the real E2E and fix the application "
                    "only if that reveals a defect. Do not remove the scenario label or test.")
        if issue.get('source_quote'):
            message += ' Source: ' + issue['source_quote']
    if issue.get('reason') == 'requirement_test_scope_evidence_missing':
        message += (f"; case {issue['source_case_id']}; source {issue['source_path']}; "
                    f"missing verifiable self-test evidence ({issue['evidence_reason']}). "
                    "In the original source-case test body, directly await the source-required "
                    f"main-scoped click on the {issue['role']} named {issue['name']!r}, then run real E2E. "
                    "Preserve the scenario label, prerequisites and remaining steps. Do not delete "
                    "the test, change the requirement or append an unrelated click to satisfy this check. "
                    "Application correctness remains unverified; fix the application only if "
                    "the real E2E reveals a defect. Source: " + issue['source_quote'])
    return message

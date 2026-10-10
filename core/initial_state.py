"""Source-indexed, conservative initial-state extraction. No domain entity list.

This is a lexical evidence pass, not a claim to understand arbitrary prose.
Every recognized state clause remains visible even when no supported fact exists.
Competition wording lives in competition/, not in this executor.
"""
from hashlib import sha256
import json
from pathlib import Path
import re
from core.profile import profile_enabled

POLICY = Path(__file__).resolve().parents[1] / 'competition/initial-state.json'
QUOTED = re.compile(r'"([^"\n]+)"|“([^”\n]+)”|`([^`\n]+)`|「([^」\n]+)」|\'([^\'\n]+)\'')


def clauses(text):
    """Do not split punctuation inside literal names or email addresses."""
    masked = list(text)
    for match in QUOTED.finditer(text):
        masked[match.start():match.end()] = 'x' * (match.end() - match.start())
    start = 0
    for boundary in re.finditer(r';|；|\n|。|[.!?](?=\s+[A-Z]|$)', ''.join(masked)):
        end = boundary.end()
        if text[start:end].strip():
            yield start, text[start:end].strip()
        start = end
    if text[start:].strip():
        yield start, text[start:].strip()


def _entity(prefix):
    # Entity/field nouns are captured from the current text. Grammar only.
    prefix = re.sub(r'\b(?:named|called|titled|labeled|with (?:the )?name)\s*$', '', prefix, flags=re.I).strip()
    english = re.search(r'([A-Za-z][\w-]*(?:\s+(?:name|number|address))?)\s*$', prefix)
    if english:
        word = english[1].casefold()
        return None if word in {'a','an','the','and','or','to','of','for','with','is','are','as','one'} else word
    chinese = re.search(r'(?:系统(?:已|中)?(?:不存在|存在|包含|预置|没有)|已存在|不存在|包含|预置|没有|名为|名称为)([^，。；："“「`]+)$', prefix)
    return chinese[1].strip() if chinese else None


def compile_initial_state(requirements, cases=None):
    result = {'facts': [], 'constraints': [], 'sources': [], 'unverified': [], 'excluded': [], 'input_coverage': [],
              'scenario_preconditions': [], 'scenario_seeds': [],
              'scope': 'description facts and scoped scenario seed declarations; relationships and unparsed prose are not certified'}
    if not profile_enabled():
        return {**result, 'enabled': False}
    policy = json.loads(POLICY.read_text())
    if not policy['enabled']:
        return {**result, 'enabled': False}
    regex = {key: re.compile(value, re.I) for key, value in policy.items() if key.endswith('_pattern')}
    sources = [(key, 'fields.description', str(item['fields'].get('description') or ''), 'description', None)
               for key, item in requirements.items()]
    for key, case in (cases or {}).items():
        for i, step in enumerate(case.get('scenario', {}).get('steps', [])):
            phase = str(step.get('keyword', '')).upper()
            if phase in {'GIVEN', 'WHEN', 'THEN'}:
                sources.append((case['req_id'], f'cases.{key}.steps.{i}.content', str(step.get('content') or ''), phase, key))
    for req_id, path, text, phase, case_id in sources:
        result['input_coverage'].append({'req_id':req_id, 'source_path':path, 'phase':phase,
                                         'classification':'scanned_by_configured_grammar_not_semantically_certified'})
        section = False
        previous_offset = 0
        for offset, quote in clauses(text):
            if '\n\n' in text[previous_offset:offset]:
                section = False
            previous_offset = offset
            masked = QUOTED.sub(lambda m: 'x' * len(m[0]), quote)
            explicit = regex['explicit_section_pattern'].search(masked)
            if case_id is not None:
                # A declaration can occur after an action/result in any phase.
                # Keep its complete relationship text and scenario ownership;
                # never merge potentially conflicting scenario states into one DB.
                preprovisioned = (regex['preprovisioned_given_pattern'].search(masked)
                                  if phase == 'GIVEN' else None)
                # Only an adjectival mention still needs an existence check.
                # Affirmative provisioning can include related absent records.
                if preprovisioned and (regex['conditional_pattern'].search(masked[:preprovisioned.start()])
                                       or preprovisioned.lastgroup == 'adjectival'
                                       and regex['negative_existence_pattern'].search(masked)):
                    preprovisioned = None
                if explicit or preprovisioned:
                    # A pre-provisioned subject or its access prerequisites can
                    # precede the verb: retain the whole state clause verbatim.
                    seed_quote = quote[explicit.start():] if explicit else quote
                    source_id = sha256(f'{req_id}:{path}:{offset}:{seed_quote}'.encode()).hexdigest()[:16]
                    result['scenario_seeds'].append({'id': source_id, 'req_id': req_id,
                        'source_path': path, 'phase': phase, 'source_quote': seed_quote,
                        'case_id': case_id, 'classification': 'explicit_scenario_seed'})
                elif phase == 'GIVEN':
                    source_id = sha256(f'{req_id}:{path}:{offset}:{quote}'.encode()).hexdigest()[:16]
                    result['scenario_preconditions'].append({'id': source_id, 'req_id': req_id,
                        'source_path': path, 'phase': phase, 'source_quote': quote, 'case_id': case_id,
                        'classification': 'scenario_setup_not_global_seed'})
                continue
            if explicit:
                section = True
            state = regex['state_pattern'].search(masked)
            initial = explicit or state or section or phase == 'GIVEN'
            if not initial:
                continue
            source_id = sha256(f'{req_id}:{path}:{offset}:{quote}'.encode()).hexdigest()[:16]
            source = {'id': source_id, 'req_id': req_id, 'source_path': path, 'phase': phase, 'source_quote': quote}
            if not explicit and state and regex['conditional_pattern'].search(masked[:state.start()]):
                result['excluded'].append({**source, 'reason': 'conditional_behavior_not_initial_state'})
                continue
            body = quote[explicit.end():] if explicit else quote
            # Ignore preceding descriptive UI prose; terminal "exists" refers
            # backwards to its subject and must retain that subject.
            if not explicit and state and not re.fullmatch(r'(?:already )?exists?[.!]?', state[0], re.I):
                body = quote[state.start():]
            if regex['action_prefix_pattern'].search(body.strip()):
                result['excluded'].append({**source, 'reason': 'action_not_initial_state'})
                continue
            # Links/images are references, not database values.
            body = re.sub(r'!\[[^\]]*\]\([^)]*\)', '', body)
            result['sources'].append(source)
            found = 0
            ignored_secret = False
            unsupported = False
            previous_end = 0
            subject = None
            in_modifier = False
            for match in QUOTED.finditer(body):
                prefix = body[previous_end:match.start()]
                previous_end = match.end()
                name = next(v for v in match.groups() if v is not None)
                if regex['independent_subject_pattern'].search(prefix):
                    in_modifier = False
                if subject and (in_modifier or regex['modifier_pattern'].search(prefix)):
                    in_modifier = True
                    contains = regex['contains_modifier_pattern'].fullmatch(prefix)
                    if contains and subject['predicate'] == 'exists':
                        constraint_id = sha256(f'{source_id}:{match.start()}:contains'.encode()).hexdigest()[:16]
                        result['constraints'].append({**source, 'id': constraint_id, 'source_id': source_id,
                            'subject_id': subject['id'], 'predicate': 'contains_any',
                            'fields': re.split(r'\s+or\s+', contains['fields'], flags=re.I), 'value': name})
                    else:
                        unsupported = True
                    continue
                entity = _entity(prefix)
                if not entity or regex['unsupported_entity_pattern'].search(entity):
                    unsupported = True
                    continue
                if regex['sensitive_field_pattern'].search(entity):
                    ignored_secret = True
                    continue
                name = next(v for v in match.groups() if v is not None)
                predicate = 'absent' if regex['negative_pattern'].search(prefix) else 'exists'
                fact_id = sha256(f'{source_id}:{match.start()}:{name}'.encode()).hexdigest()[:16]
                subject = {**source, 'id': fact_id, 'source_id': source_id,
                           'entity': entity, 'name': name, 'predicate': predicate}
                result['facts'].append(subject)
                found += 1
            if not found or unsupported or ignored_secret:
                result['unverified'].append({**source, 'diagnostic_kind': 'lexical_uncertainty', 'reason': 'credential_requires_behavior_check' if ignored_secret
                                             else 'initial_claim_has_unsupported_details'})
    result['enabled'] = True
    return result

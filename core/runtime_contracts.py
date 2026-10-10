"""Extract source-owned names, actions and explicit roles for static checks."""
from hashlib import sha256
import json
from pathlib import Path
import re
from core.profile import profile_enabled


def runtime_policy():
    if not profile_enabled(): return {}
    return json.loads((Path(__file__).parents[1]/'competition/runtime-checks.json').read_text())


def compile_runtime_contracts(acceptance):
    policy=runtime_policy()
    if not policy.get('enabled'): return []
    after_role=re.compile(policy['name_before_role_pattern'],re.I)
    before_role=re.compile(policy['role_before_name_pattern'],re.I)
    action_pattern=re.compile(policy['action_before_name_pattern'],re.I)
    negative=re.compile(policy['negated_action_pattern'],re.I)
    sources=[(key,'description',str(req['fields'].get('description') or ''))
             for key,req in acceptance['requirements'].items()]
    for key,case in acceptance.get('cases',{}).items():
        for i,step in enumerate(case.get('scenario',{}).get('steps',[])):
            sources.append((case['req_id'],f'{key}.steps.{i}',str(step.get('content') or '')))
    result={}
    for req,path,text in sources:
        sidebar=re.search(policy['navigation_collapse_pattern'],text,re.I)
        if sidebar and not re.search(policy['navigation_collapse_negative_pattern'],sidebar[0],re.I):
            key=sha256(f'{req}:navigation_collapse:{path}'.encode()).hexdigest()[:16]
            result[key]={'id':key,'req_id':req,'kind':'navigation_collapse','role':'unknown','name':None,
                         'source_path':path,'source_quote':text}
        for opening,closing in policy['quote_pairs']:
            pattern=re.compile(re.escape(opening)+r'([^\n]+?)'+re.escape(closing))
            for match in pattern.finditer(text):
                prefix,suffix=text[:match.start()],text[match.end():]
                explicit=after_role.search(suffix) or before_role.search(prefix)
                action=action_pattern.search(prefix)
                if not explicit and not action: continue
                if action and negative.search(prefix[:action.start()]): continue
                role=policy['roles'][explicit['role'].lower()] if explicit else 'unknown'
                name=match[1]
                source_action=policy['actions'][action['action'].lower()] if action else None
                # Different actions retain separate provenance; never silently turn
                # a role inferred from generated DOM into an input requirement.
                identity=f'{req}:{role}:{name}' + (f':{source_action}' if source_action else '')
                key=sha256(identity.encode()).hexdigest()[:16]
                source={'source_path':path,'source_quote':text}
                if key in result:
                    if source not in result[key]['sources']: result[key]['sources'].append(source)
                else:
                    result[key]={'id':key,'req_id':req,'role':role,'name':name,'action':source_action,
                                 **source,'sources':[source]}
    explicit=[c for c in result.values() if c['role']!='unknown']
    for contract in result.values():
        if contract['role']!='unknown' or contract.get('kind'): continue
        owners={contract['req_id'],*acceptance['requirements'][contract['req_id']].get('ancestors',[])}
        evidence=[c for c in explicit if c['req_id'] in owners and c['name']==contract['name']]
        roles={c['role'] for c in evidence}
        if evidence:
            contract['role_evidence']=[{k:c[k] for k in ('req_id','role','source_path','source_quote')} for c in evidence]
        if len(roles)==1: contract['role']=next(iter(roles))
        elif len(roles)>1: contract['role_conflict']=sorted(roles)
    return list(result.values())

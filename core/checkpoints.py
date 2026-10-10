"""Bounded static checks: no installation, server, database mutation, or model call."""
import json
from pathlib import Path
import subprocess
import time
from arcbench_security import child_env
from core.profile import profile_policy
from core.ui_names import check_names, check_semantics, load_name_acceptance, semantic_issue_message


def module_checkpoint(project: Path, evidence: Path, deadline: float, *, manual_contract_checks=False):
    if manual_contract_checks is not False:
        raise ValueError('Manual contract bindings are not supported')
    policy=profile_policy()
    report={'status':'disabled','messages':[], 'scope':'static source checks; no build or browser execution'}
    if not policy.get('module_checks'):
        return report
    evidence.mkdir(parents=True,exist_ok=True)
    started=time.monotonic()
    try:
        if not (project/'frontend/node_modules/@babel/parser').exists():
            report.update(status='deferred',messages=['Static checks deferred until frontend dependencies exist; final validation will run them.'])
        else:
            output=evidence/'observations.json'
            result=subprocess.run(['node',str(Path(__file__).with_name('ui_names.cjs')),str(project),str(output)],
                cwd=project,env=child_env(),capture_output=True,text=True,timeout=max(.1,min(20,deadline-time.monotonic())))
            if result.returncode:
                report.update(status='failed',messages=['Static source parse failed: '+result.stderr[-2000:]])
            else:
                observations=json.loads(output.read_text())
                acceptance=load_name_acceptance(project)
                reviews=json.loads((project/'.haiknow/ui-name-review.json').read_text())
                if not isinstance(reviews,list) or any(not isinstance(r,dict) for r in reviews):
                    raise ValueError('ui-name-review.json must be an array of individual explanations')
                visual=json.loads((project/'.haiknow/visual-text.json').read_text())
                report['names']=check_names(acceptance,observations,reviews,visual)
                current=json.loads((project/'.haiknow/acceptance.json').read_text())
                report['semantics']=check_semantics(acceptance,observations,scope_acceptance=current)
                issues=report['names']['issues']+report['semantics']['issues']
                report.update(status='issues' if issues else 'checked',messages=[
                    semantic_issue_message(i)
                    for i in issues[:policy['max_checkpoint_messages']]])
                room = max(0, policy['max_checkpoint_messages'] - len(report['messages']))
                promoted = {(i.get('source_case_id'), i.get('role'), i.get('name'))
                            for i in report['semantics']['issues']
                            if i['reason'] == 'requirement_test_scope_evidence_missing'}
                reviews = []
                for item in report['semantics']['unverified']:
                    if item.get('required_scope') == 'main':
                        if (item.get('source_case_id'), item.get('role'), item.get('name')) in promoted:
                            continue
                        reviews.append(f"Review only, not a gate: case {item['source_case_id']}; "
                            f"source {item['source_path']}; {item['reason']}. "
                            "Scope remains unverified; inspect the original scenario and test evidence. "
                            'Source: ' + item['source_quote'])
                    elif item.get('source') == 'jsx-interaction':
                        reviews.append(f"Review only, not a gate: {item.get('file')}:{item.get('line')} "
                            f"{item['reason']}. If this opens content, verify keyboard and card surface "
                            'activation; delegation/analytics handlers need no replacement.')
                report['messages'].extend(reviews[:room])
    except (OSError,ValueError,TypeError,KeyError,subprocess.TimeoutExpired) as exc:
        report.update(status='deferred',messages=['Static checkpoint incomplete: '+str(exc)[:1500]])
    report['seconds']=round(time.monotonic()-started,3)
    (evidence/'checkpoint.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    return report

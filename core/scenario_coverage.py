"""Match current source scenarios to real Playwright executions, not test counts."""
from core.scenario_delta import scenario_index


def execution_evidence(report):
    """Return canonical test entries and structural/actual-outcome problems."""
    entries, issues = [], []
    if not isinstance(report, dict):
        return entries, [{'reason': 'invalid_report'}]

    def visit(suite):
        if not isinstance(suite, dict):
            issues.append({'reason': 'invalid_suite'})
            return
        for spec in suite.get('specs', []):
            if not isinstance(spec, dict) or not isinstance(spec.get('tests'), list) or not spec['tests']:
                issues.append({'reason': 'spec_without_executions'})
                continue
            for test in spec['tests']:
                location = {'file': spec.get('file'), 'line': spec.get('line'), 'title': spec.get('title')}
                if not isinstance(test, dict):
                    issues.append({**location, 'reason': 'invalid_test_execution'})
                    continue
                results = test.get('results')
                passed = (test.get('expectedStatus') == 'passed' and test.get('status') == 'expected'
                          and isinstance(results, list) and bool(results)
                          and all(isinstance(row, dict) and row.get('status') == 'passed'
                                  and not row.get('error') and not row.get('errors') for row in results))
                entries.append({**location, 'passed': passed, 'annotations': test.get('annotations', [])})
                if not passed:
                    issues.append({**location, 'reason': 'test_did_not_actually_pass'})
        children = suite.get('suites', [])
        if not isinstance(children, list):
            issues.append({'reason': 'invalid_suites'})
        else:
            for child in children:
                visit(child)

    visit(report)
    stats = report.get('stats')
    if not isinstance(stats, dict) or any(type(stats.get(key)) is not int or stats[key] < 0
                                        for key in ('expected', 'unexpected', 'flaky', 'skipped')):
        issues.append({'reason': 'invalid_execution_stats'})
    else:
        total = sum(stats[key] for key in ('expected', 'unexpected', 'flaky', 'skipped'))
        if not entries or total != len(entries):
            issues.append({'reason': 'execution_count_mismatch', 'reported': total, 'actual': len(entries)})
        if stats['unexpected'] or stats['flaky'] or stats['skipped']:
            issues.append({'reason': 'failed_flaky_or_skipped_tests'})
    if report.get('errors'):
        issues.append({'reason': 'runner_errors'})
    return entries, issues


def audit_report(report, acceptance, previous):
    """Only source-body deltas require labels; unchanged regressions still run."""
    index = scenario_index(acceptance, previous)
    required = index['required_case_ids']
    current = set(index['cases'])
    entries, issues = execution_evidence(report)
    covered, locations = set(), {}
    for entry in entries:
        annotations = entry['annotations']
        location = {key: entry[key] for key in ('file', 'line', 'title')}
        if not isinstance(annotations, list) or any(not isinstance(row, dict) for row in annotations):
            issues.append({**location, 'reason': 'invalid_annotations'})
            continue
        # The result-level annotation list repeats this metadata. Do not merge it.
        labels = [row for row in annotations if row.get('type') == 'source-case']
        if len(labels) > 1:
            issues.append({**location, 'reason': 'multiple_source_case_annotations'})
            continue
        if not labels:
            continue
        case_id = labels[0].get('description')
        if not isinstance(case_id, str) or case_id not in current:
            issues.append({**location, 'reason': 'unknown_current_case_id', 'case_id': case_id})
            continue
        if entry['passed']:
            covered.add(case_id)
            locations.setdefault(case_id, []).append(location)
    missing = [case_id for case_id in required if case_id not in covered]
    if missing:
        issues.append({'reason': 'missing_scenario_executions', 'case_ids': missing})
    return {'passed': not issues, 'scope': index['scope'], 'required_case_ids': required,
            'covered_case_ids': sorted(covered), 'missing_case_ids': missing,
            'executions': locations, 'issues': issues,
            'limitation': 'Execution labels detect missing scenarios; they do not prove assertion semantics, '
                          'description-only changes or inherited constraints are satisfied.'}

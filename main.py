from __future__ import annotations

import argparse
from hashlib import sha256
import json
import os
import shutil
import shlex
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from openai_codex import ApprovalMode, Codex, CodexConfig
from arcbench_agent_runtime import AgentRuntime
from core.validation import CheckFailure, remaining, verify_project, verify_delivery
from core.time_budget import RunDeadlineExceeded, time_limit
from agents.model.visual_transport import VisualTransport
from arcbench_security import child_env, isolated_sdk
from agents.sdk_diagnostics import SDKDiagnostics

import yaml
from core.requirements import compile_acceptance, module_index
from core.profile import profile_instructions, profile_enabled, profile_policy
from core.checkpoints import module_checkpoint
from core.repository import initialize_repository, commit_snapshot
from core.references import images_enabled, reference_contexts


DEFAULT_TIME_BUDGET_SECONDS = None


@dataclass(frozen=True)
class RequirementModule:
    index: int
    total: int
    node_id: str
    name: str
    subtree: dict[str, Any]
    root_context: dict[str, Any]
    acceptance: dict[str, Any] = field(default_factory=dict)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Implement an ARC-Bench task with Codex.")
    parser.add_argument("requirement_path", help="Directory containing requirements.yaml.")
    parser.add_argument("--output-dir", required=True, help="Target directory for the generated project.")
    parser.add_argument(
        "--type",
        dest="task_type",
        default=os.environ.get("ARCBENCH_TASK_TYPE", "web"),
        help="Task type supplied by ARC-Bench.",
    )
    return parser.parse_args()


def copy_template_contents(template_dir: Path, output_dir: Path) -> None:
    """Fill missing starter files without replacing existing application code."""
    if not template_dir.is_dir():
        raise FileNotFoundError(f"Starter template directory not found: {template_dir}")
    if output_dir.is_symlink() or (output_dir.exists() and not output_dir.is_dir()):
        raise ValueError(f'Refusing to initialize a non-directory or symlink: {output_dir}')
    output_dir.mkdir(parents=True, exist_ok=True)
    for source in sorted(template_dir.iterdir()):
        if source.name in {"template.yaml", "node_modules", "dist", "build", ".git", ".venv", "venv", "__pycache__", ".DS_Store", ".env", ".arc-test-db"} or source.name.startswith('.env.') or source.name.endswith(('.pyc', '.egg-info')):
            continue
        if source.is_symlink():
            raise ValueError(f"Symlink in starter is not supported: {source}")
        destination = output_dir / source.name
        if destination.is_symlink():
            raise ValueError(f"Refusing to initialize through symlink: {destination}")
        if source.is_dir():
            if destination.exists() and not destination.is_dir():
                raise ValueError(f"Expected directory: {destination}")
            copy_template_contents(source, destination)
        elif not destination.exists():
            shutil.copy2(source, destination)


def copy_skills_to_output(skills_dir: Path, output_dir: Path) -> Path:
    """Make the bundled ARC-Bench skills available inside Codex's workspace."""
    if not skills_dir.is_dir():
        raise FileNotFoundError(f"Skills directory not found: {skills_dir}")
    destination = output_dir / ".codex" / "skills"
    allowed = ('arcbench-traceability', 'arcbench-checkpoint', 'arcbench-runtime-signals')
    destination.mkdir(parents=True, exist_ok=True)
    if any(p.name not in allowed for p in destination.iterdir()):
        raise ValueError('Unexpected skills already present in output; use a clean workspace')
    for name in allowed:
        copy_template_contents(skills_dir/name, destination/name)
    return destination


def initialize_project(agent_root: Path, output_dir: Path) -> None:
    copy_template_contents(agent_root / 'template', output_dir)
    if profile_enabled():
        copy_template_contents(agent_root / 'competition/scaffold', output_dir)


def load_root_modules(requirements_dir: Path) -> list[RequirementModule]:
    requirements_path = requirements_dir / "requirements.yaml"
    if not requirements_path.is_file():
        raise FileNotFoundError(f"requirements.yaml not found: {requirements_path}")

    payload = yaml.safe_load(requirements_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or str(payload.get("id") or "").strip() != "ROOT":
        raise ValueError("requirements.yaml must contain a ROOT mapping")

    children = payload.get("children", [])
    if not isinstance(children, list) or not all(isinstance(item, dict) for item in children):
        raise ValueError("ROOT children must be a list of mappings")
    if not children:
        raise ValueError("ROOT must contain at least one child module")

    seen_ids: set[str] = set()
    seen_objects: set[int] = set()
    def check_node(node: dict[str, Any]) -> None:
        if id(node) in seen_objects:
            raise ValueError("Cyclic or reused requirement node")
        seen_objects.add(id(node))
        node_id = str(node.get('id') or node.get('req_id') or '').strip()
        if not node_id:
            raise ValueError("Every requirement node must have an id or req_id")
        if node_id in seen_ids:
            raise ValueError(f"Duplicate requirement id: {node_id}")
        seen_ids.add(node_id)
        descendants = node.get('children') or []
        if not isinstance(descendants, list) or not all(isinstance(n, dict) for n in descendants):
            raise ValueError(f"Invalid children for {node_id}")
        for descendant in descendants:
            check_node(descendant)
    check_node(payload)
    compiled = compile_acceptance(payload, allow_external_dependencies=bool(
        profile_policy().get('external_dependencies_as_prerequisites', False)))
    by_id = {str(c.get("id") or c.get("req_id")).strip():c for c in children}
    children = [by_id[key] for key in compiled["module_order"]]
    root_context = {key: value for key, value in payload.items() if key != 'children'}
    root_context['module_index'] = [
        {key: child[key] for key in ('id', 'req_id', 'name', 'dependencies') if key in child}
        for child in children
    ]
    modules: list[RequirementModule] = []
    for index, subtree in enumerate(children, start=1):
        node_id = str(subtree.get("id") or subtree.get("req_id") or "").strip()
        if not node_id:
            raise ValueError(f"ROOT child {index} has no id")
        modules.append(
            RequirementModule(
                index=index,
                total=len(children),
                node_id=node_id,
                name=str(subtree.get("name") or node_id).strip(),
                subtree=subtree,
                root_context=root_context,
                acceptance=module_index(compiled, node_id),
            )
        )
    return modules


def _codex_config(CodexConfig: Any, home: Path, output_dir: Path,
                  transport: VisualTransport | None = None, *, role="coding") -> Any:
    from core.evaluator_isolation import configure_launcher, EvaluatorIsolationError
    output_dir, home = Path(output_dir).resolve(), Path(home).resolve()
    if not home.is_relative_to(output_dir):
        raise EvaluatorIsolationError('SDK home must be inside its writable project')
    home.mkdir(parents=True, exist_ok=True)
    env = child_env()
    env['HOME'] = str(home)
    env['CODEX_HOME'] = str(home)
    for key, directory in {'TMPDIR':'tmp', 'XDG_CACHE_HOME':'cache', 'npm_config_cache':'npm-cache'}.items():
        target = home/directory; target.mkdir(exist_ok=True)
        env[key] = str(target)
    browser_cache = output_dir/'.arc/haiknow/browser-cache'
    if browser_cache.is_dir():
        env['PLAYWRIGHT_BROWSERS_PATH'] = str(browser_cache)
    elif Path('/ms-playwright').is_dir():
        # Match the controller's successful organizer-image browser probe.
        env['PLAYWRIGHT_BROWSERS_PATH'] = '/ms-playwright'
    # This gateway implements Responses, not OpenAI encrypted remote compaction.
    # A named custom provider keeps SDK text compaction on ordinary Responses.
    overrides: list[str] = ['model_provider="arcbench"', 'cli_auth_credentials_store="file"',
                            'model_providers.arcbench.name="ARC-Bench"',
                            'model_providers.arcbench.wire_api="responses"',
                            'model_providers.arcbench.env_key="OPENAI_API_KEY"',
                            'model_providers.arcbench.requires_openai_auth=false',
                            'model_providers.arcbench.supports_websockets=false',
                            'web_search="disabled"', 'features.multi_agent=false',
                            'features.apps=false', 'features.plugins=false', 'mcp_servers={}',
                            'approval_policy="never"', 'sandbox_mode="danger-full-access"']
    base_url = transport.base_url if transport else os.environ.get("OPENAI_BASE_URL", "").strip()
    if transport:
        env['OPENAI_API_KEY'] = transport.local_key
    if base_url:
        env["OPENAI_BASE_URL"] = base_url
        overrides.append(f"model_providers.arcbench.base_url={json.dumps(base_url)}")
    return configure_launcher(CodexConfig(env=env, cwd=str(output_dir), config_overrides=tuple(overrides)), output_dir, role)


def _developer_instructions(skills_dir: Path) -> str:
    from core.evaluator_isolation import access_instructions
    rules = (Path(__file__).resolve().parent / 'HAIKNOW-LITE.md').read_text(encoding='utf-8')
    execution = ('Reuse this single coding thread across ROOT modules and repairs. '
                 'Maintain backend/test-e2e journeys as well as unit/API tests. The controller performs static module checkpoints, then final cold validation. '
                 )
    return rules + '\n' + profile_instructions() + '\nEffective workflow: simple. '+execution + f'''\nARC-Bench skills are available at {skills_dir}.
Read arcbench-traceability/SKILL.md to record real interface/test locations.
Use bundled scripts with --project-dir .; do not hand-write platform JSON.
The controller owns run status and validation. Do not mark global pass signals.
{access_instructions()}
Use the supplied ROOT constraints and current subtree; a fresh thread must not assume a previous thread's memory.
Reference image parsing is {'enabled for explicit functional text delegation only' if images_enabled() else 'disabled'}.
Do not OCR style references or call vision through shell. Only use view_image for a missing functional
fact explicitly delegated by the written requirement. The controller sends its original REQ context.
Controller-owned .haiknow/visual-text.json contains scoped supplemental facts and is frozen to tools.
Never edit that file or add screenshot text to acceptance.json. Unresolved image text is not a fact.
'''


def _module_prompt(
    module: RequirementModule,
    requirements_dir: Path,
    completed_ids: list[str],
    skills_dir: Path,
) -> str:
    # Reuse the coding thread; frozen source views support context recovery.
    parts = [f'Implement ROOT module {module.index}/{module.total}: {module.node_id} - {module.name}']
    if not completed_ids:
        parts += [f'Requirement directory (including referenced assets): {requirements_dir}',
                  'ROOT global constraints and sibling index apply throughout this thread:',
                  json.dumps(module.root_context, ensure_ascii=False, separators=(',', ':'))]
        parts += ['Dependencies outside the current requirement tree are required capabilities '
                  'to inspect, verify and implement if missing; they are not certified complete.']
        workspace_file = skills_dir.parent.parent / '.haiknow/workspace.json'
        if workspace_file.is_file() and json.loads(workspace_file.read_text()).get('mode') == 'incremental':
            parts += ['This output directory contains an existing application. Inspect its implementation '
                      'and regression tests first; extend it in place and preserve working features, '
                      'persistent data and prior tests. Do not recreate the application from the starter. '
                      'Database schema changes need data-preserving, repeatable migrations: delivery checks '
                      'start retained input and final databases on separate copies, then restart each copy. '
                      'When the input contains the application database, add focused Playwright journeys in '
                      'backend/test-e2e-retained/*.spec.js (or .cjs/.mjs/.ts). First assert existing baseline '
                      'objects or relations relevant to this change, then exercise the new behavior and '
                      'revisit/reload it. Use fresh browser contexts, page.goto("/") and the visible entry '
                      'required by this YAML. Do not reset the database, create replacement fixtures, import '
                      'destructive test harnesses or start servers in these tests. The controller supplies '
                      'baseURL and a running copy of the migrated input database; it does not load the '
                      'application Playwright config/globalSetup. Ordinary scripts/check.py e2e checks '
                      'remain separate and do not certify this retained-data group. '
                      'This is a new coding session, not a resume of prior completion claims. '
                      'The current YAML is authoritative for this stage; previous source descriptions '
                      'may be available in .haiknow/prior-acceptance.json for compatibility and regression.']
    else:
        parts += ['Implemented modules (validation pending): ' + ','.join(completed_ids)]
    from core.module_context import module_context_path
    context_file = skills_dir.parent.parent / module_context_path(module.node_id)
    if context_file.is_file():
        changes = json.loads(context_file.read_text()).get('evolution', {})
        # Original current subtree is already included below. Keep the prompt
        # index compact; old/current step text and inherited fields remain in
        # the immutable module view for source queries after context compaction.
        summary = {**changes, 'requirements': {
            key: {**row, 'inherited_source_changes': [ancestor['req_id'] for ancestor in
                    row.get('inherited_source_changes', [])]}
            for key, row in changes.get('requirements', {}).items()},
            'cases': {key: {k: v for k, v in row.items() if k != 'steps'} |
                      {'changed_step_indices': row.get('steps', {}).get('changed_indices')}
                      for key, row in changes.get('cases', {}).items()}}
        parts += ['Controller source-change index (not implementation coverage):',
                  json.dumps(summary, ensure_ascii=False, separators=(',', ':')),
                  'For every required_case_id owned by this module, implement its current journey '
                  'and label an actually executed normal Playwright E2E test with '
                  '{type: "source-case", description: "<current case ID>"}. '
                  'Keep existing regression tests. The final controller rejects missing executions, '
                  'unknown IDs, skips and expected failures. One test labels at most one case. '
                  'Inherited source changes remain binding even when scenario text is unchanged; '
                  'case execution coverage alone does not certify their semantics. '
                  f'Full changed-step and ancestor source text is in {module_context_path(module.node_id)}; '
                  'read it where the compact index shows a change or uncertain identity.',
                  'For explicitly paired Original/Modified Feature Description sections, implement the '
                  'modified behavior and current scenarios; retain compatible original behavior. '
                  'An unchanged source still needs compatibility checks where shared code changes. '
                  'no_prior_source means history is missing, not that functionality is absent. '
                  'Inspect existing code before editing and focus effort on current changes and actual gaps; '
                  'do not rewrite working modules just to reproduce prior requirements.']
    parts += ['Global constraints remain binding. If context was compacted or a contract is unclear, '
              'read .haiknow/contract.md, plan.md and progress.md; otherwise reuse known context.',
              'Implement the complete current subtree:',
              json.dumps(module.subtree, ensure_ascii=False, separators=(',', ':')),
              'Controller-compiled case/leaf index (not a new model task):',
              json.dumps({key:value for key,value in module.acceptance.items()
                          if key != 'runtime_contract_ids'}, ensure_ascii=False, separators=(',', ':')),
              'Use the indexed current scenarios and inherited constraints as the test oracle. '
              'Implement leaf dependencies first; when a cross-module prerequisite is unfinished, '
              'build its necessary shared boundary and revisit the full journey during integration. '
              f'Query original ancestor/dependency details with python3 scripts/contract_context.py query --module {shlex.quote(module.node_id)} --kind requirements; '
              'use --kind initial for source-derived seed facts. These lexical aids do not replace the original YAML or behavioral tests. '
              'Add focused functional tests for requirements and changed shared behavior. '
              'Record requirement IDs, interface/test locations and unresolved items briefly in progress.md.']
    if module.index == module.total:
        parts += ['This is the last module: also complete cross-module integration now. '
                  'Check the full module index for omissions, shared state, persistence, '
                  'required visual assets and negative paths. Ensure meaningful tests in '
                  + 'frontend/tests, backend/tests and backend/test-e2e before finishing. '
                  + 'The controller then runs fresh install/build/tests and dual-port checks.']
    parts += ['Dependencies and matching Chromium are already installed. Run affected checks through '
                  'python3 scripts/check.py frontend backend e2e (or one target with --file relative/test-file). '
                  'It manages build, temporary database, both ports, browser path and cleanup; reuse its successful '
                  'unchanged-source evidence. Do not hand-write server startup/kill scripts or rerun the same '
                  'successful checks with cosmetic command changes. Use --fresh only for a concrete unresolved '
                  'concern. After a package/lock change, run npm ci in that package before checks. '
                  'Final controller cold acceptance never uses this development cache.']
    parts += ['Return a brief change/evidence summary; leave no background servers.']
    return '\n\n'.join(parts)


def _failure_excerpt(error: Any, limit: int = 2800) -> str:
    value = _redact(error)
    if len(value) <= limit:
        return value
    # Preserve the initial cause/command and the final assertion; full evidence
    # remains on disk and is only read when the bounded excerpt is insufficient.
    return value[:800] + '\n[intermediate output omitted; full log available on disk]\n' + value[-(limit - 900):]


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)


def _redact(value: Any) -> str:
    text = str(value)
    for name in ('OPENAI_API_KEY', 'VISUAL_API_KEY'):
        key = os.environ.get(name, '').strip()
        if key:
            text = text.replace(key, '[REDACTED]')
    return text


def _prepare_knowledge(requirements_dir: Path, output_dir: Path,
                       modules: list[RequirementModule], *, manual_contract_checks=False) -> dict[Path, str]:
    if manual_contract_checks is not False:
        raise ValueError('Manual contract bindings are not supported by this workflow')
    knowledge = output_dir / '.haiknow'
    knowledge.mkdir(parents=True, exist_ok=True)
    source = requirements_dir / 'requirements.yaml'
    source_hash = sha256(source.read_bytes()).hexdigest()
    contract = knowledge / 'contract.md'
    contract.write_text('# Frozen requirement contract\n\n'
                        f'Source: {source}\n\nSHA256: {source_hash}\n\n'
                        'ROOT global context and module index:\n\n```json\n' +
                        json.dumps(modules[0].root_context, ensure_ascii=False, indent=2) + '\n```\n', encoding='utf-8')
    plan = knowledge / 'plan.md'
    plan.write_text('# Frozen implementation order\n\n'
                    'Follow the controller dependency order; preserve shared contracts and existing work.\n'
                    'Changes to this original scope belong in progress.md, not this file.\n\n' +
                    '\n'.join(f'{m.index}. {m.node_id}: {m.name}' for m in modules) +
                    '\n\nFinish with cross-module integration, build, real Vitest suites, '
                    'a temporary dual-port startup and Playwright journeys.\n', encoding='utf-8')
    (knowledge / 'progress.md').write_text('# Execution findings and evidence\n\n'
                                         'Implementation has not been validated yet.\n', encoding='utf-8')
    acceptance = knowledge / 'acceptance.json'
    acceptance.write_text(json.dumps(compile_acceptance(yaml.safe_load(source.read_text()),
        allow_external_dependencies=bool(profile_policy().get('external_dependencies_as_prerequisites', False))),
        ensure_ascii=False, indent=2) + '\n')
    _write_json(knowledge / 'ui-name-review.json', [])
    supplemental = knowledge / 'visual-text.json'
    _write_json(supplemental, [])
    frozen = {path: sha256(path.read_bytes()).hexdigest() for path in (source, contract, plan, acceptance, supplemental)}
    for name in ('workspace.json', 'prior-acceptance.json'):
        path = knowledge / name
        if path.is_file(): frozen[path] = sha256(path.read_bytes()).hexdigest()
    from core.database_delivery import frozen_input_files
    for path in frozen_input_files(output_dir): frozen[path] = sha256(path.read_bytes()).hexdigest()
    from core.module_context import write_module_contexts
    frozen.update(write_module_contexts(knowledge, modules, json.loads(acceptance.read_text()),
                                       manual_contract_checks=manual_contract_checks))
    return frozen


def _check_frozen(frozen: dict[Path, str]) -> None:
    for path, expected in frozen.items():
        if path.is_symlink() or not path.is_file() or sha256(path.read_bytes()).hexdigest() != expected:
            raise RuntimeError(f'Frozen input changed: {path.name}; record deviations instead')


def implement_modules(
    requirements_dir: Path,
    output_dir: Path,
    modules: list[RequirementModule],
    skills_dir: Path,
    *, time_budget: float | None = DEFAULT_TIME_BUDGET_SECONDS, repair_attempts: int | None = 2,
    model: str | None = None, run_limits: dict | None = None,
    coding_idle_seconds: float | None = 600,
) -> None:
    model = model or os.environ['MODEL'].strip()
    completed_ids: list[str] = []
    hard_deadline = float('inf') if time_budget is None else time.monotonic() + time_budget
    reserve = (run_limits or {}).get('delivery_reserve_seconds', 0) if time_budget is not None else 0
    if reserve < 0 or (time_budget is not None and reserve >= time_budget):
        raise ValueError('Delivery reserve must fit inside the remaining total budget')
    deadline = hard_deadline - reserve
    runtime = AgentRuntime.from_env(project_dir=str(output_dir))
    evidence = output_dir / '.arc' / 'haiknow'
    evidence.mkdir(parents=True, exist_ok=True)
    state: dict[str, Any] = {'version': (Path(__file__).parent / 'VERSION').read_text().strip(), 'status': 'running', 'model': model, 'time_budget_seconds': time_budget,
                             'workflow':'simple', 'phase':'implementation', 'limits':run_limits,
                             'delivery_reserve_seconds':reserve,
                             'e2e_source':'generated_with_application', 'gate_passed':None,
                             'internal_acceptance_passed':False, 'manual_contract_checks':'disabled',
                             'acceptance_scope':'cold build/startup, static source names/roles, generated unit/API/E2E; not official score or exhaustive requirement coverage',
                             'implemented_modules': completed_ids, 'turns': [], 'validations': []}
    from core.evaluator_isolation import access_policy
    state['evaluator_access'] = access_policy()
    print('[haiknow] evaluator access: '+json.dumps(state['evaluator_access']), flush=True)
    frozen = _prepare_knowledge(requirements_dir, output_dir, modules, manual_contract_checks=False)
    visual_contexts = reference_contexts(requirements_dir, json.loads((output_dir / '.haiknow/acceptance.json').read_text())) if images_enabled() else {}
    active_id: str | None = None
    runtime.traceability.init_store()
    runtime.traceability.store_requirement_tree(yaml.safe_load((requirements_dir / 'requirements.yaml').read_text()))
    runtime.events.mark_run_started('HAiKnow-Lite + Codex: local delivery checks are required')
    # Keep the generated repository independent from a developer machine's signing/hooks.
    os.environ['GIT_CONFIG_NOSYSTEM'] = '1'
    os.environ['GIT_CONFIG_GLOBAL'] = os.devnull
    _write_json(evidence / 'state.json', state)
    try:
        initialize_repository(runtime)
        with time_limit(None if time_budget is None else remaining(hard_deadline, float('inf'))):
            try:
                with time_limit(None if time_budget is None else remaining(deadline, float('inf')),
                                error_type=RunDeadlineExceeded), tempfile.TemporaryDirectory(prefix='arc-haiknow-codex-', dir=output_dir/'.arc') as home, \
                        VisualTransport(evidence, deadline, reference_contexts=visual_contexts,
                                        coding_idle_seconds=coding_idle_seconds) as transport:
                    from core.verification_guard import VerificationGuard
                    transport.verification_guard = VerificationGuard(output_dir)
                    config = _codex_config(CodexConfig, Path(home), output_dir, transport)
                    with isolated_sdk(lambda: Codex(config=config), config.env) as codex, \
                            SDKDiagnostics(codex, evidence, lambda value: _redact(value).replace(transport.local_key, '[REDACTED]'), deadline):
                        codex.login_api_key(transport.local_key)
                        instructions = _developer_instructions(skills_dir)
                        state['efficiency'] = {'developer_instruction_chars': len(instructions),
                                               'prompt_chars': 0, 'controller_turns': 0,
                                               'usage_note': 'SDK thread snapshots; do not sum cumulative totals. Characters are not tokens. Inner model requests and billing are platform-owned.'}
                        from core.evaluator_isolation import start_isolated_thread
                        thread = start_isolated_thread(codex, 'coding', evidence,
                            cwd=str(output_dir),
                            approval_mode=ApprovalMode.deny_all, model=model, ephemeral=True,
                            developer_instructions=instructions,
                        )
                        state['thread_id'] = thread.id
                        from core.simple_recovery import recover, checked_report

                        def execute_turn(prompt: str, label: str, retry: int) -> None:
                            remaining(deadline)
                            _check_frozen(frozen)
                            if retry:
                                transport.failure = None
                                prompt = ('The previous coding operation lost its transport. Inspect existing files and '
                                          '.haiknow/progress.md before continuing the unfinished work; preserve completed '
                                          'changes and tests. Do not blindly repeat prior tool effects. Original task:\n' + prompt)
                            budget_note = ('no controller time limit' if time_budget is None else
                                           f'{int(deadline - time.monotonic())} seconds remaining')
                            print(f'[codex] {label}; {budget_note}', flush=True)
                            state['efficiency']['prompt_chars'] += len(prompt)
                            state['efficiency']['controller_turns'] += 1
                            _write_json(evidence / 'state.json', state)
                            transport.verification_guard.begin_turn(label)
                            try:
                                result = thread.run(prompt)
                            except RunDeadlineExceeded:
                                raise
                            except Exception:
                                if transport.failure:
                                    # Convert only the proxy's explicit deadline signal.
                                    # Auth/permission/unknown failures stay terminal at the boundary.
                                    if str(transport.failure).endswith(': total run time budget exhausted'):
                                        remaining(deadline)
                                    raise transport.failure from None
                                raise
                            if transport.failure:
                                if str(transport.failure).endswith(': total run time budget exhausted'):
                                    remaining(deadline)
                                raise transport.failure
                            _check_frozen(frozen)
                            remaining(deadline)
                            state['visual'] = transport.summary()
                            state['efficiency']['verification_guard'] = list(transport.verification_guard.events)
                            _check_frozen(frozen)
                            supplemental = output_dir / '.haiknow/visual-text.json'
                            _write_json(supplemental, transport.supplement)
                            frozen[supplemental] = sha256(supplemental.read_bytes()).hexdigest()
                            status = getattr(result.status, 'value', result.status)
                            record: dict[str, Any] = {'label': label, 'status': status,
                                                      'prompt_chars': len(prompt),
                                                      'response': _redact(result.final_response or '')}
                            if result.usage is not None:
                                record['usage'] = result.usage.model_dump(mode='json')
                                state['efficiency']['latest_sdk_usage'] = record['usage']
                            state['turns'].append(record)
                            _write_json(evidence / 'state.json', state)
                            if result.error is not None or status != 'completed':
                                raise RuntimeError(f'Codex {label}: status={status}; {_redact(result.error)}')

                        def turn(prompt: str, label: str) -> None:
                            recover(label, lambda retry: execute_turn(prompt, label, retry), evidence, deadline,
                                    integrity=lambda: _check_frozen(frozen), redact=_redact)

                        def validate(check, label, records, **options):
                            def once(retry):
                                directory = evidence / (label + (f'-recovery-{retry}' if retry else ''))
                                report = check(output_dir, directory, deadline, **options)
                                records.append(report)
                                _write_json(evidence / 'state.json', state)
                                return checked_report(report)
                            return recover(label, once, evidence, deadline,
                                           integrity=lambda: _check_frozen(frozen), redact=_redact)

                        pending_checks = []
                        state['module_checks'] = []
                        for module in modules:
                            active_id = module.node_id
                            runtime.events.mark_implementation_started(active_id)
                            prompt = _module_prompt(module, requirements_dir, completed_ids, skills_dir)
                            if pending_checks:
                                prompt += '\n\nController static findings from the previous module (fix while continuing this turn; full report in .arc/haiknow/module-checks):\n' + '\n'.join(pending_checks)
                            turn(prompt,
                                 f'module {module.index}/{module.total}: {active_id}')
                            checkpoint = module_checkpoint(output_dir, evidence / 'module-checks' / str(module.index), deadline,
                                manual_contract_checks=False)
                            pending_checks = checkpoint['messages']
                            state['module_checks'].append({'module_id': active_id, **checkpoint})
                            completed_ids.append(active_id)
                            runtime.events.mark_implementation_done(active_id, 'Coding turn completed; final validation still pending')
                            commit_snapshot(runtime, f'feat: implement requirement {active_id}')
                            active_id = None
                            _write_json(evidence / 'state.json', state)

                        attempt = 0
                        while True:
                            remaining(deadline)
                            _check_frozen(frozen)
                            state['phase']='validation'
                            _write_json(evidence / 'state.json', state)
                            report = validate(verify_project, f'validation-{attempt}', state['validations'], manual_contract_checks=False)
                            _check_frozen(frozen)
                            can_repair = repair_attempts is None or attempt < repair_attempts
                            if report['passed']:
                                runtime.events.mark_test_passed('ROOT', 'Internal build, Vitest and Playwright checks passed; official grading is separate')
                                partial = any(report.get(kind, {}).get('coverage_status') == 'partial' or
                                              report.get(kind, {}).get('unverified')
                                              for kind in ('initial_records', 'runtime_ui'))
                                state['status'] = 'completed_with_issues' if partial else 'completed'
                                state['internal_acceptance_passed'] = not partial
                                if partial:
                                    state['unresolved_error'] = 'Executable checks passed; independent initial-state or runtime UI coverage is partial. See initial-records.json and runtime-ui.json.'
                                commit_snapshot(runtime, 'test: verified build, local tests and dual-port startup')
                                _write_json(evidence / 'state.json', state)
                                runtime.events.mark_run_completed(state.get('unresolved_error') or 'Internal delivery checks passed; generated app ready for external evaluation')
                                return
                            if not report['passed']:
                                runtime.events.mark_test_failed('ROOT', 'Internal verification failed; see .arc/haiknow reports')
                            if not can_repair:
                                # 只在新的构建和双端口检查成功后交付，原测试失败记录完整保留。
                                # SDK中断、取消和输入篡改不会进入这条质量降级路径。
                                delivery = validate(verify_delivery, 'delivery-check', state.setdefault('delivery_attempts', []))
                                state['delivery'] = delivery
                                remaining(deadline)
                                _check_frozen(frozen)
                                if delivery['passed']:
                                    state['status'] = 'completed_with_issues'
                                    state['internal_acceptance_passed'] = False
                                    state['unresolved_error'] = _redact(report['error'])
                                    commit_snapshot(runtime, 'chore: deliver runnable application with unresolved checks')
                                    _write_json(evidence / 'state.json', state)
                                    runtime.events.mark_run_completed(
                                        'Runnable application delivered with unresolved internal checks; '
                                        'see state.json; no internal or official pass claimed')
                                    return
                                raise CheckFailure(_redact(report['error']))
                            feedback = report.get('error') or ''
                            state['phase']='repair'
                            turn('Repair attempt ' + str(attempt + 1) + '/' + (str(repair_attempts) if repair_attempts is not None else 'unlimited') + '. '
                                 + 'The controller actually ran the delivery checks and they failed. ' +
                                 'Full evidence is under ' + str(evidence / f'validation-{attempt}') + '; '
                                 'open only the relevant failing log if the excerpt is insufficient. '
                                 'Diagnose the smallest failing path. Preserve all requirements, tests, '
                                 'reporting configuration and passing behavior. Do not fabricate reports or '
                                 'disable a test to pass. Fix lockfile/dependency problems when applicable. '
                                 'Leave no servers running. Findings:\n' + _failure_excerpt(feedback),
                                 f'repair {attempt + 1}')
                            attempt += 1
            except RunDeadlineExceeded:
                if not reserve or time.monotonic() < deadline:
                    raise
                # SDK and its proxy have left their contexts. No model/retry is
                # allowed here; use only the time still left in the original budget.
                remaining(hard_deadline)
                _check_frozen(frozen)
                state.update(phase='delivery', stop_reason='time_budget',
                             unfinished_modules=[m.node_id for m in modules if m.node_id not in completed_ids],
                             internal_acceptance_passed=False,
                             unresolved_error='Work time budget exhausted; unfinished work and internal failures remain unverified')
                if 'transport' in locals():
                    state['visual'] = transport.summary()
                _write_json(evidence / 'state.json', state)
                print('[haiknow] Work deadline reached; no more model calls; checking existing delivery', flush=True)
                delivery = verify_delivery(output_dir, evidence / 'deadline-delivery', hard_deadline)
                state['delivery'] = delivery
                _write_json(evidence / 'state.json', state)
                remaining(hard_deadline)
                _check_frozen(frozen)
                if not delivery['passed']:
                    raise CheckFailure(_redact(delivery.get('error') or 'Timed-out application is not runnable'))
                if active_id:
                    runtime.events.mark_implementation_failed(active_id, 'Stopped at work deadline; module incomplete')
                commit_snapshot(runtime, 'chore: deliver runnable application after work deadline')
                remaining(hard_deadline)
                state['status'] = 'completed_with_issues'
                _write_json(evidence / 'state.json', state)
                runtime.events.mark_run_completed(
                    'Work deadline reached; existing application passed build/startup only; '
                    'unfinished work recorded in state.json; official evaluation remains separate')
                return
    except BaseException as exc:
        state['status'] = 'failed'
        if 'transport' in locals():
            state['visual'] = transport.summary()
        state['error'] = _redact(f'{type(exc).__name__}: {exc}')
        _write_json(evidence / 'state.json', state)
        if active_id:
            runtime.events.mark_implementation_failed(active_id, state['error'][:1000])
        runtime.events.mark_run_failed(state['error'][:1000])
        raise


def main() -> int:
    args = parse_args()
    requirements_dir = Path(args.requirement_path).expanduser().resolve()
    output_path = Path(args.output_dir).expanduser()
    if output_path.is_symlink():
        raise ValueError('Output directory must not be a symlink')
    output_dir = output_path.resolve()
    if not requirements_dir.is_dir():
        raise FileNotFoundError(f"Requirement directory not found: {requirements_dir}")

    started = time.monotonic()
    agent_root = Path(__file__).resolve().parent
    # Legacy environment caps must not silently override the centralized policy.
    if any(os.environ.get(key, '').strip() for key in ('HAIKNOW_TIME_BUDGET_SECONDS', 'HAIKNOW_REPAIR_ATTEMPTS')):
        raise ValueError('Configure limits in runtime-policy.yaml; legacy cap variables are unsupported')
    for protected in (agent_root, requirements_dir):
        if output_dir == protected or output_dir in protected.parents or protected in output_dir.parents:
            raise ValueError('Output must be separate from the agent source and requirement directories')
    for key in ('OPENAI_API_KEY', 'OPENAI_BASE_URL', 'MODEL'):
        if not os.environ.get(key, '').strip():
            raise ValueError(f'Missing platform environment variable: {key}')
    from core.runtime_config import load_policy
    policy = load_policy()
    from core.evaluator_isolation import access_policy
    access_policy()
    from core.simple_workflow import run_simple
    run_simple(requirements_dir, output_dir, policy=policy, started=started)
    return 0


if __name__ == "__main__":
    try:
        with time_limit(10800):  # Whole process anti-hang, not model budget.
            raise SystemExit(main())
    except Exception as exc:
        print(f'[haiknow] FAILED: {_redact(exc)}', file=sys.stderr, flush=True)
        raise SystemExit(1)

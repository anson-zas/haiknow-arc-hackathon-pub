"""Start a new run on existing application code without reusing agent state."""
from datetime import datetime, timezone
from copy import deepcopy
from hashlib import sha256
import json
import os
from pathlib import Path
import tempfile


# Never move .arc itself: the platform may already be writing its runner log.
ARCHIVED_PATHS = {
    '.haiknow': 'knowledge',
    '.arc/haiknow': 'evidence',
    '.arc/traceability': 'traceability',
    '.arc/local-check': 'development-checks',
    '.codex': 'assistant-codex',
    '.agents': 'assistant-agents',
}


def _check_directory(path, *, recursive=False):
    if path.is_symlink():
        raise ValueError(f'Output metadata path must not be a symlink: {path}')
    if path.exists() and not path.is_dir():
        raise ValueError(f'Output metadata path must be a directory: {path}')
    if recursive and path.is_dir():
        for directory, dirs, files in os.walk(path, followlinks=False):
            for name in dirs + files:
                item = Path(directory) / name
                if item.is_symlink():
                    raise ValueError(f'Output metadata path must not be a symlink: {item}')


def merge_requirement_sources(previous, current):
    """Combine descriptions only; a current REQ replaces all its prior cases."""
    result = {'requirements': {}, 'cases': {}}
    for value in (previous, current):
        if (not isinstance(value, dict) or
                not isinstance(value.get('requirements'), dict) or
                not isinstance(value.get('cases'), dict) or
                any(not isinstance(row, dict) or not isinstance(row.get('fields'), dict)
                    for row in value['requirements'].values()) or
                any(not isinstance(row, dict) or row.get('req_id') not in value['requirements']
                    for row in value['cases'].values())):
            raise ValueError('Invalid previous requirement source')
        replaced = set(value['requirements'])
        result['requirements'].update({key: deepcopy({field: row[field]
            for field in ('ancestors', 'module_id', 'fields') if field in row})
            for key, row in value['requirements'].items()})
        result['cases'] = {key: row for key, row in result['cases'].items()
                           if row['req_id'] not in replaced}
        result['cases'].update({key: deepcopy({field: row[field]
            for field in ('source', 'req_id', 'module_id', 'scenario', 'description') if field in row})
            for key, row in value['cases'].items()})
    return result


def _prior_sources(project):
    result = {'requirements': {}, 'cases': {}}
    for name in ('prior-acceptance.json', 'acceptance.json'):
        path = project / '.haiknow' / name
        if path.is_file():
            result = merge_requirement_sources(result, json.loads(path.read_text(encoding='utf-8')))
    return result


def prepare_workspace(project, requirements_file):
    """Validate first; archive agent-owned metadata, never application files."""
    project = Path(project)
    _check_directory(project)
    _check_directory(project / '.arc')
    for relative in (*ARCHIVED_PATHS, '.arc/haiknow-history'):
        _check_directory(project / relative, recursive=True)
    prior = _prior_sources(project)
    existing = project.is_dir() and any(path.name not in {'.arc', '.haiknow', '.codex', '.agents', '.git'}
                                        for path in project.iterdir())
    source_hash = sha256(Path(requirements_file).read_bytes()).hexdigest()
    pending = [(project / relative, name) for relative, name in ARCHIVED_PATHS.items()
               if (project / relative).exists()]
    archive = None
    if pending:
        history = project / '.arc/haiknow-history'
        history.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-')
        archive = Path(tempfile.mkdtemp(prefix=stamp, dir=history))
        moved = []
        try:
            for source, name in pending:
                target = archive / name
                source.rename(target)
                moved.append((source, target))
        except BaseException:
            for source, target in reversed(moved): target.rename(source)
            archive.rmdir()
            raise
    knowledge = project / '.haiknow'
    knowledge.mkdir(parents=True, exist_ok=True)
    record = {
        'mode': 'incremental' if existing else 'fresh',
        'archive': str(archive.relative_to(project)) if archive else None,
        'requirement_sha256': source_hash,
        'prior_source_scope': 'previous requirement descriptions only; no previous pass is reused',
    }
    from core.database_delivery import capture_input
    record['database_input'] = capture_input(project)
    (knowledge / 'workspace.json').write_text(json.dumps(record, indent=2) + '\n', encoding='utf-8')
    if prior['requirements']:
        (knowledge / 'prior-acceptance.json').write_text(
            json.dumps(prior, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return record

#!/usr/bin/env python3
"""Query frozen requirement sources for the current module.

Uses Python's standard library only. This is an authoring aid, not an acceptance
validator: final controller checks and real browser execution remain required.
"""
import argparse
from hashlib import sha256
import json
from pathlib import Path


def load_view(project, module_id):
    index = json.loads((project / '.haiknow/modules/index.json').read_text())
    row = index['modules'].get(module_id)
    if row is None:
        raise ValueError('Unknown module: ' + module_id)
    path = project / row['path']
    if path.is_symlink() or not path.resolve().is_relative_to(project / '.haiknow/modules'):
        raise ValueError('Module context path is outside its frozen directory')
    raw = path.read_bytes()
    if sha256(raw).hexdigest() != row['sha256']:
        raise ValueError('Frozen module context changed')
    view = json.loads(raw)
    if view['schema'] != 'module-source-context/v1' or view['module_id'] != module_id:
        raise ValueError('Module context identity mismatch')
    return view


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project-dir', default='.')
    commands = parser.add_subparsers(dest='command', required=True)
    query = commands.add_parser('query')
    query.add_argument('--module', required=True)
    query.add_argument('--kind', choices=['summary', 'initial', 'requirements', 'changes', 'all'], default='summary')
    args = parser.parse_args()
    try:
        project = Path(args.project_dir).resolve()
        view = load_view(project, args.module)
        if args.kind == 'all':
            result = view
        else:
            fields = {
                'summary': ['schema','module_id','module_leaf_ids','leaf_ids','dependencies','full_inputs','validation_scope'],
                'initial': ['module_id','initial_state','full_inputs'],
                'changes': ['module_id','evolution','full_inputs'],
                'requirements': ['module_id','requirements','cases','dependencies','references','full_inputs'],
            }[args.kind]
            result = {key: view[key] for key in fields}
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(json.dumps({'status': 'rejected', 'error': str(exc)}, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())

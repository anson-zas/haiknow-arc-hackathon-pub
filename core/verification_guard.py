"""End tool use after repeated validation of unchanged sources; never fake results.

This limits repeated model requests, not a single shell command's internal loop.
Full controller validation remains independent and always runs afterwards.
"""
from hashlib import sha256
import json
import os
from pathlib import Path
import re
from core.profile import profile_enabled

EXCLUDED = {'.git','.arc','.codex','node_modules','dist','build','coverage',
            'test-results','playwright-report','__pycache__'}


def source_fingerprint(root):
    digest = sha256()
    for directory, dirs, files in os.walk(root, followlinks=False):
        base = Path(directory)
        dirs[:] = sorted(n for n in dirs if n not in EXCLUDED and not n.startswith('.env')
                         and not (base/n).is_symlink())
        for name in sorted(files):
            path = base/name
            if (path.is_symlink() or name.startswith('.env') or name.endswith(
                    ('.log','.db','.db-wal','.db-shm','.sqlite','.sqlite3','.pyc')) or
                    path == root/'.haiknow/progress.md'):
                continue
            digest.update(str(path.relative_to(root)).encode())
            with path.open('rb') as stream:
                while chunk := stream.read(65536): digest.update(chunk)
    return digest.hexdigest()


class VerificationGuard:
    def __init__(self, project, policy=None):
        self.project = Path(project)
        if policy is None:
            policy = json.loads((Path(__file__).parents[1]/'competition/efficiency.json').read_text()) if profile_enabled() else {}
        self.limit = int(policy.get('unchanged_validation_limit',0))
        self.repetition_scope = policy.get('repetition_scope', 'all_validation_commands')
        self.pattern = re.compile(policy.get('validation_command_pattern', r'(?!)'), re.I)
        self.seen = set()
        self.events = []
        self.begin_turn('initial')

    def begin_turn(self, label):
        self.label, self.digest, self.repeats, self.stopped = label, None, 0, False
        self.command_counts = {}

    def apply(self, payload):
        if not self.limit or not isinstance(payload.get('input'), list): return
        completed = {i.get('call_id') for i in payload['input'] if isinstance(i,dict) and i.get('type')=='function_call_output'}
        commands = []
        for item in payload['input']:
            if not isinstance(item,dict) or item.get('type')!='function_call': continue
            key = item.get('call_id')
            if key in self.seen or key not in completed: continue
            self.seen.add(key)
            if item.get('name','').split('.')[-1] not in {'exec_command','shell','shell_command'}: continue
            try:
                args = json.loads(item.get('arguments','{}'))
                cmd = args.get('cmd',args.get('command',''))
                if isinstance(cmd,list): cmd = ' '.join(cmd)
                if isinstance(cmd,str) and self.pattern.search(cmd):
                    # Keep shell syntax intact: whitespace inside quoted strings
                    # can change meaning. The digest does not persist raw commands.
                    signature = sha256(json.dumps([item.get('name'),args.get('workdir'),
                        args.get('cwd'),cmd],ensure_ascii=False).encode()).hexdigest()
                    commands.append(signature)
            except (ValueError,TypeError,AttributeError): continue
        if commands and not self.stopped:
            try:
                current = source_fingerprint(self.project)
            except OSError:
                self.digest, self.repeats = None, 0
                self.command_counts = {}
                return  # A concurrent write is not evidence of unchanged code.
            if current != self.digest:
                self.digest, self.repeats = current, 0
                self.command_counts = {}
            for signature in commands:
                self.command_counts[signature] = self.command_counts.get(signature,0)+1
            self.repeats = (max(self.command_counts.values()) if self.repetition_scope=='command'
                            else sum(self.command_counts.values()))
            if self.repeats >= self.limit:
                self.stopped = True
                self.events.append({'turn':self.label, 'source_sha256':current,
                                    'unchanged_validation_commands':self.repeats,
                                    'repetition_scope':self.repetition_scope,
                                    'distinct_validation_commands':len(self.command_counts),
                                    'action':'request_summary_without_tools'})
        if self.stopped:
            payload['tools'] = []
            payload['tool_choice'] = 'none'
            payload['input'].append({'role':'developer','content':[{'type':'input_text','text':
                'Controller: repeated validation commands without source changes reached the configured limit. '
                'End this coding turn with a concise summary and remaining issues. This is not validation success. '
                'The controller will independently build and test, and may request repairs. Do not emit tool calls.'}]})

"""Keep disposable test output out of application snapshots, not out of the workspace."""
import re


REPORT_DIRS = ('test-results', 'playwright-report', 'blob-report')
START = '# >>> haiknow-snapshot >>>'
END = '# <<< haiknow-snapshot <<<'


def _prepare_snapshot(runtime):
    # Restore official rules too if a coding turn rewrote .gitignore.
    path = runtime.git.ensure_arc_gitignore()
    content = re.sub(r'(?ms)^' + re.escape(START) + r'\n.*?^' + re.escape(END) + r'\n?',
                     '', path.read_text(encoding='utf-8')).rstrip()
    block = '\n'.join((START, *(name + '/' for name in REPORT_DIRS), END))
    path.write_text(content + '\n\n' + block + '\n', encoding='utf-8')

    if not (runtime.paths.project_dir / '.git').exists():
        return
    # Ignore rules do not affect already tracked files in an evolution input.
    # Only remove known disposable reports from the index; retain disk evidence
    # and every prior commit. Git errors still propagate to the controller.
    paths = runtime.git.run(['ls-files', '-z', '--',
                             *(':(glob)**/' + name + '/**' for name in REPORT_DIRS)]).stdout
    tracked = [name for name in paths.split('\0') if name]
    for offset in range(0, len(tracked), 100):
        runtime.git.run(['rm', '--cached', '--force', '--ignore-unmatch', '--',
                         *tracked[offset:offset + 100]])


def initialize_repository(runtime):
    _prepare_snapshot(runtime)
    runtime.git.ensure_repo()


def commit_snapshot(runtime, message):
    _prepare_snapshot(runtime)
    return runtime.git.commit(message)

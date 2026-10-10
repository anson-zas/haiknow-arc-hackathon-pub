"""Dependency and browser preparation for the single coding workflow."""
from pathlib import Path
from core.validation import CheckFailure, run_command


def prepare_dependencies(evidence, deadline, *, project):
    """Install application dependencies and verify the matching browser."""
    workspace = Path(project)
    for part in ('backend', 'frontend'):
        target = workspace/part; target.mkdir(parents=True, exist_ok=True)
        run_command(['npm','ci','--include=dev','--no-audit','--no-fund'], target,
                    evidence/f'tools-{part}.log', deadline, cap=600)
    modules = workspace/'backend/node_modules'
    # Match the installed Playwright, including its headless-shell revision.
    # Do not download a second browser when the organizer image already works.
    probe = """const {chromium}=require(process.argv[1]);
(async()=>{const browser=await chromium.launch({headless:true});await browser.close();})()
.catch(error=>{console.error(String(error));process.exitCode=
 String(error).toLowerCase().includes("executable doesn't exist")?86:1;});"""
    cache = evidence/'browser-cache'
    def check_browser(log):
        return run_command(['node', '-e', probe, str(modules/'@playwright/test')],
                           workspace/'backend', evidence/log, deadline, cap=60,
                           browser_cache=cache if cache.is_dir() else None)
    try:
        check_browser('tools-browser-probe.log')
    except CheckFailure as exc:
        # Only our missing-executable exit code permits a download. A broken
        # system library, architecture mismatch or timeout remains a real error.
        if not str(exc).startswith('Command failed (86):'):
            raise
        run_command(['node', str(modules/'@playwright/test/cli.js'), 'install', 'chromium'],
                    workspace/'backend', evidence/'tools-browser.log', deadline,
                    cap=600, browser_cache=cache)
        check_browser('tools-browser-installed-probe.log')
    return modules, workspace/'frontend'

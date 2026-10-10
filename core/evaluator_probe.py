"""Fixed no-model child probe; only booleans leave the Landrun process."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import urlopen


def main(args):
    checks = {}
    def check(name, operation):
        try: checks[name] = operation() is True
        except Exception: checks[name] = False
    def readable(path):
        try:
            with open(path, 'rb') as handle: handle.read(1)
            return True
        except PermissionError: return False
    def write():
        Path('write-check').write_text('probe')
        return True
    def denied(operation):
        try: operation(); return False
        except PermissionError: return True
    def project_truncate():
        Path('truncate-check').write_text('sample')
        os.truncate('truncate-check', 2)
        return Path('truncate-check').read_text() == 'sa'
    def sqlite():
        with sqlite3.connect('probe.db') as db:
            db.execute('create table records(n integer)'); db.execute('insert into records values (7)')
            return db.execute('select n from records').fetchone() == (7,)
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200); self.end_headers(); self.wfile.write(b'<h1>LOCAL_PROBE_OK</h1>')
        def log_message(self, *args): pass
    def network(browser=False):
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        worker = threading.Thread(target=server.serve_forever); worker.start()
        try:
            url = 'http://127.0.0.1:'+str(server.server_port)
            if not browser:
                with urlopen(url, timeout=5) as response: return response.read() == b'<h1>LOCAL_PROBE_OK</h1>'
            modules = Path(args['node_modules'])
            caches = ([Path(args['browser_cache'])] if args.get('browser_cache') else []) + [
                      modules.parents[2]/'browser-cache', Path('/ms-playwright'),
                      Path.home()/'.cache/ms-playwright', Path.home()/'Library/Caches/ms-playwright']
            env = dict(os.environ)
            cache = next((p for p in caches if p.is_dir()), None)
            if cache: env['PLAYWRIGHT_BROWSERS_PATH'] = str(cache)
            script = '''const {chromium}=require(process.argv[1]); (async()=>{let b;
try {b=await chromium.launch({headless:true, ...(process.argv[3]?{executablePath:process.argv[3]}:{})});const p=await b.newPage();
await p.goto(process.argv[2]); if(await p.locator('h1').textContent()!=='LOCAL_PROBE_OK')process.exitCode=1;
}finally {if(b)await b.close();}})().catch(e=>{console.error(String(e).slice(-1800));process.exitCode=1});'''
            child = subprocess.run(['node','-e',script,str(modules/'@playwright/test'),url,args.get('browser_path') or ''],
                                   env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, timeout=60)
            if child.returncode: sys.stderr.write(child.stderr[-1800:])
            return child.returncode == 0
        finally: server.shutdown(); server.server_close(); worker.join()
    if args['node_modules']:
        check('chromium_local_http', lambda: network(True))
    else:
        check('outside_read', lambda: readable(args['witness']))
        check('outside_relative_read', lambda: readable(os.path.relpath(args['witness'])))
        check('outside_symlink_read', lambda: readable(args['link']))
        check('outside_write_denied', lambda: denied(lambda: Path(args['witness']).write_text('forbidden')))
        check('outside_delete_denied', lambda: denied(lambda: Path(args['witness']).unlink()))
        check('outside_truncate_denied', lambda: denied(lambda: os.truncate(args['witness'], 0)))
        check('outside_symlink_write_denied', lambda: denied(lambda: Path(args['link']).write_text('forbidden')))
        check('outside_create_denied', lambda: denied(lambda: Path(args['witness']).with_name('created').write_text('forbidden')))
        check('allowed_read', lambda: readable(args['requirements']))
        check('project_write', write); check('project_truncate', project_truncate)
        check('sqlite', sqlite); check('loopback', network)
        check('npm', lambda: subprocess.run(['npm','test','--offline','--no-audit','--no-fund'],
              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30).returncode == 0)
    print(json.dumps(checks))


if __name__ == '__main__': main(json.loads(sys.argv[1]))

"""Serve the existing local editor with a Python-backed layout Worker."""
from __future__ import annotations

import argparse
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from math import isfinite
import mimetypes
import os
from pathlib import Path
import re
import secrets
import signal
import threading
from urllib.parse import unquote, urlsplit

from .cli import ADAPTER_ROOT, REPOSITORY_ROOT, find_node, write_json
from .candidate_schedule import POLICY as CANDIDATE_SCHEDULE
from .editable_adapter import validate_layout_budget_capability
from .web_jobs import JobManager, WebJobConfig

API = '/api/netlist2schematic'


class EditorAssets:
    """A read-only virtual build namespace; no edits to generated target files."""
    def __init__(self, root, token):
        self.root = Path(root).resolve()
        self.index = (self.root/'index.html').read_text(encoding='utf8')
        candidates = list((self.root/'assets').glob('auto-layout.worker-*.js'))
        if len(candidates) != 1:
            raise ValueError('Expected exactly one native Auto Layout Worker in this editor build')
        self.original_worker = candidates[0].name
        if not any(self.original_worker in p.read_text(encoding='utf8')
                   for p in (self.root/'assets').glob('*.js') if p != candidates[0]):
            raise ValueError('The deployed editor does not reference its known Auto Layout Worker')
        worker = (ADAPTER_ROOT/'web/auto-layout.worker.mjs').read_text(encoding='utf8')
        self.worker = worker.replace('__N2S_TOKEN__', token).encode('utf8')
        converters = list((self.root/'assets').glob('convert-import-sources-*.js'))
        if len(converters) != 1:
            raise ValueError('Expected exactly one native source-preparation module')
        self.original_converter = converters[0].name
        self.native_converter = converters[0].read_text(encoding='utf8')
        exports = re.findall(r'export\s*\{\s*[$\w]+\s+as\s+([$\w]+)\s*\}\s*;?', self.native_converter)
        if len(exports) != 1:
            raise ValueError('Unsupported native source-preparation exports; import adaptation needs review')
        wrapper = (ADAPTER_ROOT/'web/spice-import.mjs').read_text(encoding='utf8')
        self.converter = wrapper.replace('__N2S_PREPARE_EXPORT__', exports[0]).encode('utf8')
        self.spice_compat = (ADAPTER_ROOT/'spice-compat.mjs').read_bytes()
        digest = hashlib.sha256(self.index.encode()+worker.encode()+self.converter+self.spice_compat).hexdigest()[:12]
        self.prefix = '/__n2s/'+digest+'-'+secrets.token_hex(5)+'/'

    def _rewrite(self, text):
        text = text.replace(self.original_worker, 'python-auto-layout.worker.js')
        text = text.replace(self.original_converter, 'python-convert-import-sources.js')
        for quote in ('"', "'", '`'):
            text = text.replace(quote+'/assets/', quote+self.prefix+'assets/')
        return text

    def read(self, pathname):
        if pathname.startswith('/__n2s/'):
            if not pathname.startswith(self.prefix):
                raise FileNotFoundError('Previous adapter instance; reload the editor')
            pathname = '/'+pathname[len(self.prefix):]
        if pathname in ('/assets/python-auto-layout.worker.js', '/assets/'+self.original_worker):
            return self.worker, 'text/javascript; charset=utf-8', 'no-store'
        if pathname in ('/assets/python-convert-import-sources.js', '/assets/'+self.original_converter):
            return self.converter, 'text/javascript; charset=utf-8', 'no-store'
        if pathname == '/assets/native-convert-import-sources.js':
            return self._rewrite(self.native_converter).encode('utf8'), 'text/javascript; charset=utf-8', 'no-store'
        if pathname == '/assets/spice-compat.mjs':
            return self.spice_compat, 'text/javascript; charset=utf-8', 'no-store'
        relative = pathname.lstrip('/') or 'index.html'
        target = (self.root/relative).resolve()
        if not target.is_relative_to(self.root):
            raise FileNotFoundError('Path escapes the editor root')
        if not target.is_file():
            if target.suffix or pathname.startswith('/api/'):
                raise FileNotFoundError(pathname)
            target = self.root/'index.html'
        data = target.read_bytes()
        if target.name == 'index.html':
            text = self._rewrite(self.index).replace('<title>Auto Canvas</title>',
                '<title>Auto Canvas · Netlist2Schematic</title>')
            data = text.encode('utf8')
        elif target.suffix == '.js':
            data = self._rewrite(data.decode('utf8')).encode('utf8')
        kind = mimetypes.guess_type(target.name)[0] or 'application/octet-stream'
        if target.suffix in ('.js', '.mjs'):
            kind = 'text/javascript; charset=utf-8'
        if target.suffix == '.webmanifest':
            kind = 'application/manifest+json'
        return data, kind, 'no-store' if target.suffix in ('.html','.js') else 'no-cache'


class LayoutServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, *, config, web_root, token=None, max_body_bytes=32*1024*1024,
                 manager=None):
        self.token = token or secrets.token_urlsafe(32)
        self.assets = EditorAssets(web_root, self.token)
        self.manager = manager or JobManager(config)
        self.config = config
        self.max_body_bytes = max_body_bytes
        super().__init__(address, Handler)

    def server_close(self):
        try:
            self.manager.close()
        finally:
            super().server_close()


class Handler(BaseHTTPRequestHandler):
    server_version = 'Netlist2SchematicLocal/1'

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def _reply(self, code, value, *, kind='application/json; charset=utf-8', cache='no-store'):
        data = value if isinstance(value, bytes) else (json.dumps(value, ensure_ascii=False)+'\n').encode('utf8')
        self.send_response(code)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', cache)
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Cross-Origin-Resource-Policy', 'same-origin')
        self.send_header('Cross-Origin-Opener-Policy', 'same-origin-allow-popups')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Content-Security-Policy', "default-src 'self'; img-src 'self' blob: data:; "
            "style-src 'self'; script-src 'self' 'wasm-unsafe-eval'; worker-src 'self' blob:; "
            "connect-src 'self' https://rich-netlist.github.io")
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(data)

    def _allowed(self, *, secret=False):
        port = self.server.server_address[1]
        hosts = {f'127.0.0.1:{port}', f'localhost:{port}'}
        if self.headers.get('Host') not in hosts:
            self._reply(403, dict(message='Invalid local Host'))
            return False
        origin = self.headers.get('Origin')
        if origin is not None and origin not in {'http://'+host for host in hosts}:
            self._reply(403, dict(message='Cross-origin requests are refused'))
            return False
        if self.headers.get('Sec-Fetch-Site') == 'cross-site':
            self._reply(403, dict(message='Cross-site requests are refused'))
            return False
        supplied = self.headers.get('X-Netlist2Schematic-Token', '')
        if secret and not hmac.compare_digest(supplied.encode('utf8'), self.server.token.encode('ascii')):
            self._reply(403, dict(message='The local layout session expired; refresh this page'))
            return False
        return True

    def do_GET(self):
        if not self._allowed():
            return
        path = unquote(urlsplit(self.path).path)
        if path in ('/healthz', API+'/health'):
            c = self.server.config
            self._reply(200, dict(status='ok', engine='Netlist2Schematic', protocolVersion=1,
                pythonLayout=True, iterations=c.iterations, seed=c.seed, engineSeconds=c.engine_seconds,
                resultPolicy='editable',totalSeconds=c.engine_seconds,primaryCandidates=['2','3'],
                candidateSchedule=CANDIDATE_SCHEDULE,
                layoutBudgetPolicy=c.layout_budget,
                cancellationLeaseSeconds=c.lease_seconds))
        elif path == '/api/session':
            self._reply(200, dict(mode='local'))
        elif re.fullmatch(re.escape(API)+r'/jobs/[0-9a-f]{32}', path):
            if not self._allowed(secret=True):
                return
            try:
                self._reply(200, self.server.manager.get(path.rsplit('/',1)[1]))
            except KeyError:
                self._reply(404, dict(message='Unknown or retired layout job'))
        elif path.startswith('/api/'):
            self._reply(404, dict(message='Unknown local endpoint'))
        else:
            try:
                data, kind, cache = self.server.assets.read(path)
                self._reply(200, data, kind=kind, cache=cache)
            except (OSError, ValueError):
                self._reply(404, dict(message='Asset not found; refresh if the layout service restarted'))

    do_HEAD = do_GET

    def do_POST(self):
        if not self._allowed(secret=True):
            return
        path = urlsplit(self.path).path
        if path == API+'/shutdown':
            self._reply(200, dict(status='stopping'))
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        if path != API+'/jobs':
            self._reply(404, dict(message='Unknown local endpoint'))
            return
        if self.headers.get('Content-Type','').split(';',1)[0].strip() != 'application/json':
            self._reply(415, dict(message='Use application/json'))
            return
        try:
            size = int(self.headers.get('Content-Length','-1'))
        except ValueError:
            size = -1
        if size < 0 or self.headers.get('Transfer-Encoding'):
            self._reply(411, dict(message='A fixed Content-Length is required'))
            return
        if size > self.server.max_body_bytes:
            self.close_connection = True
            self._reply(413, dict(message='Native Project exceeds the configured request size limit'))
            return
        try:
            body = self.rfile.read(size)
            if len(body) != size:
                raise ValueError('Incomplete request')
            request = json.loads(body)
            self._reply(202, self.server.manager.submit(request))
        except (ValueError, UnicodeError, RecursionError) as exc:
            self._reply(400, dict(message=str(exc)))
        except RuntimeError as exc:
            self._reply(429, dict(message=str(exc)))
        except OSError:
            self._reply(500, dict(message='Could not create the local layout job'))

    def do_DELETE(self):
        if not self._allowed(secret=True):
            return
        path = urlsplit(self.path).path
        if not re.fullmatch(re.escape(API)+r'/jobs/[0-9a-f]{32}', path):
            self._reply(404, dict(message='Unknown local endpoint'))
            return
        try:
            self._reply(200, self.server.manager.cancel_job(path.rsplit('/',1)[1]))
        except KeyError:
            self._reply(404, dict(message='Unknown or retired layout job'))

    def do_OPTIONS(self):
        self._reply(405, dict(message='Cross-origin API access is not enabled'))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project-root', type=Path, required=True)
    parser.add_argument('--engine-root', type=Path, default=REPOSITORY_ROOT)
    parser.add_argument('--runtime', type=Path, default=REPOSITORY_ROOT/'.cache/auto-canvas-runtime')
    parser.add_argument('--web-root', type=Path)
    parser.add_argument('--node', type=Path)
    parser.add_argument('--port', type=int, default=4174)
    parser.add_argument('--engine-seconds', type=float, default=300,
                        help='Total running-job budget including conversion and verification; queue wait excluded')
    parser.add_argument('--iterations', type=int, default=20000)
    parser.add_argument('--seed', type=int, default=20261029)
    parser.add_argument('--layout-budget',choices=('legacy','adaptive'),default='adaptive')
    parser.add_argument('--lease-seconds', type=float, default=8)
    parser.add_argument('--jobs-root', type=Path, default=REPOSITORY_ROOT/'.cache/auto-canvas-web/jobs')
    parser.add_argument('--state-file', type=Path)
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535 or args.iterations < 0 or not isfinite(args.engine_seconds) or args.engine_seconds <= 0 or not isfinite(args.lease_seconds) or args.lease_seconds < 3:
        parser.error('Invalid port, iteration count, engine budget or lease')
    target, runtime = args.project_root.resolve(), args.runtime.resolve()
    try:
        validate_layout_budget_capability(args.engine_root.resolve(),args.layout_budget)
    except ValueError as exc:
        parser.error(str(exc))
    manifest = runtime/'runtime-manifest.json'
    if not manifest.is_file() or json.loads(manifest.read_text(encoding='utf8'))['target_lock_sha256'] != hashlib.sha256((target/'pnpm-lock.yaml').read_bytes()).hexdigest():
        parser.error('Prepare the matching isolated native runtime first')
    config = WebJobConfig(target, args.engine_root.resolve(), runtime,
        find_node(target,args.node), args.jobs_root.resolve(), args.engine_seconds,
        args.iterations, args.seed, args.lease_seconds,layout_budget=args.layout_budget)
    server = LayoutServer(('127.0.0.1',args.port),config=config,
        web_root=args.web_root or target/'.local/preview/web')
    origin = 'http://127.0.0.1:'+str(server.server_address[1])
    state_path = args.state_file.resolve() if args.state_file else None
    if state_path:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        write_json(state_path, dict(pid=os.getpid(),origin=origin,token=server.token,
            module=str(Path(__file__).resolve()),engine='Netlist2Schematic',
            project_root=str(target),engine_root=str(config.engine_root)))
    def interrupt(*_):
        threading.Thread(target=server.shutdown,daemon=True).start()
    signal.signal(signal.SIGINT,interrupt)
    if hasattr(signal,'SIGTERM'):
        signal.signal(signal.SIGTERM,interrupt)
    print('Netlist2Schematic editor ready: '+origin+'/',flush=True)
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()
        if state_path and state_path.is_file():
            saved=json.loads(state_path.read_text(encoding='utf8'))
            if saved.get('pid')==os.getpid():
                state_path.unlink()


if __name__ == '__main__':
    main()

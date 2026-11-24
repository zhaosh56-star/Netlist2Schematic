"""Start/stop the independent local web host without changing shell policy."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import socket
import subprocess
import sys
from time import monotonic, sleep
import urllib.error
import urllib.request
import webbrowser

from .cli import ADAPTER_ROOT, REPOSITORY_ROOT


def api(origin, path, *, token=None, method='GET'):
    request = urllib.request.Request(origin+path, method=method,
        headers={'X-Netlist2Schematic-Token':token} if token else {})
    # Local calls must not be sent through a user's HTTP proxy.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=2) as response:
        return json.load(response)


def run(args):
    root = REPOSITORY_ROOT/'.cache/auto-canvas-web'
    root.mkdir(parents=True, exist_ok=True)
    state_path = root/f'port-{args.port}.json'
    origin = f'http://127.0.0.1:{args.port}'
    expected_module = (ADAPTER_ROOT/'web_server.py').resolve()

    def state():
        if not state_path.is_file():
            return None
        saved = json.loads(state_path.read_text(encoding='utf8'))
        if saved.get('origin') != origin or Path(saved.get('module','')).resolve() != expected_module:
            raise ValueError('The saved host identity does not match this adapter')
        return saved

    saved = state()
    if args.action == 'stop':
        if not saved:
            print('No running service is recorded for '+origin)
            return
        try:
            api(origin,'/api/netlist2schematic/shutdown',token=saved['token'],method='POST')
        except (OSError, urllib.error.HTTPError) as exc:
            raise ValueError('Could not stop the recorded service; no process was killed') from exc
        deadline=monotonic()+12
        while state_path.exists() and monotonic()<deadline:
            sleep(0.1)
        if state_path.exists():
            raise ValueError('The service is still stopping; inspect its log')
        print('The Python layout web service is stopped.')
        return

    occupied=False
    with socket.socket() as probe:
        probe.settimeout(0.5)
        occupied=probe.connect_ex(('127.0.0.1',args.port))==0
    if occupied:
        if not saved:
            raise ValueError('This port belongs to another host; choose another port')
        for key,want in (('project_root',args.project_root),('engine_root',args.engine_root)):
            if saved.get(key) and Path(saved[key]).resolve()!=want.resolve():
                raise ValueError('A service with different source roots is running; stop it before changing roots')
        health=api(origin,'/api/netlist2schematic/health')
        if health.get('engine')!='Netlist2Schematic':
            raise ValueError('Another host is using this port; it was left running')
    else:
        command=[sys.executable,'-X','utf8','-B','-m','integrations.auto_canvas_huyangc.web_server',
            '--project-root',str(args.project_root.resolve()),'--engine-root',str(args.engine_root.resolve()),
            '--port',str(args.port),'--state-file',str(state_path)]
        options=dict(cwd=REPOSITORY_ROOT,stdin=subprocess.DEVNULL,close_fds=True)
        if sys.platform=='win32':
            options['creationflags']=getattr(subprocess,'CREATE_NO_WINDOW',0)
        else:
            options['start_new_session']=True
        error_log=root/f'port-{args.port}-error.log'
        with (root/f'port-{args.port}.log').open('w',encoding='utf8') as out, error_log.open('w',encoding='utf8') as err:
            process=subprocess.Popen(command,stdout=out,stderr=err,**options)
        deadline=monotonic()+15
        while monotonic()<deadline:
            if process.poll() is not None:
                raise ValueError('The layout service did not start; see '+str(error_log))
            try:
                if api(origin,'/api/netlist2schematic/health').get('engine')=='Netlist2Schematic':
                    break
            except (OSError, urllib.error.HTTPError):
                sleep(0.15)
        else:
            raise ValueError('The layout service is not ready; see '+str(error_log))
    print('Netlist2Schematic editor: '+origin+'/editor')
    if args.action=='open':
        webbrowser.open(origin+'/editor')


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('start','open','stop'))
    parser.add_argument('--project-root',type=Path,required=True)
    parser.add_argument('--engine-root',type=Path,default=REPOSITORY_ROOT)
    parser.add_argument('--port',type=int,default=4174)
    args=parser.parse_args(argv)
    if not 1<=args.port<=65535:
        parser.error('Port must be between 1 and 65535')
    try:
        run(args)
    except (OSError,ValueError,KeyError) as exc:
        parser.exit(2,f'Web host: {exc}\n')


if __name__=='__main__':
    main()

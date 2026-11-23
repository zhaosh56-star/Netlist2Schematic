"""Bound only an owned CLI process tree, including Windows venv children."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import signal
import subprocess
import sys

from netlist2schematic.flow.diagram_budget import DEFAULT_SECONDS, validate_seconds

WORKER_ENV = 'NETLIST2SCHEMATIC_BUDGET_WORKER'


def stop_tree(process):
    if os.name == 'nt':
        # The PID comes directly from our Popen; never search for or kill all
        # Python processes. Windows venv launchers have a child interpreter.
        subprocess.run([str(Path(os.environ['SystemRoot'])/'System32/taskkill.exe'),
                        '/PID', str(process.pid), '/T', '/F'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.poll() is None:
        process.kill()
    process.wait()


def run_bounded(command, *, seconds, **kwargs):
    """Return an exit code, or 124 after reaping the owned tree on timeout."""
    validate_seconds(seconds)
    options = dict(kwargs)
    if os.name == 'nt':
        options.setdefault('creationflags', getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    else:
        options['start_new_session'] = True
    process = subprocess.Popen(command, **options)
    try:
        return process.wait(timeout=seconds or None)
    except subprocess.TimeoutExpired:
        stop_tree(process)
        return 124
    except BaseException:
        stop_tree(process)
        raise


def command_main(main, script, argv=None):
    """Keep the public Python API in-process; supervise actual CLI invocations."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    if os.environ.get(WORKER_ENV) == '1' or any(a in ('-h', '--help') for a in arguments):
        return main(arguments)
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--time-seconds', type=float, default=DEFAULT_SECONDS)
    parsed, _ = parser.parse_known_args(arguments)
    try:
        seconds = validate_seconds(parsed.time_seconds)
    except ValueError:
        return main(arguments)  # The ordinary CLI prints its usage/error.
    if seconds == 0:
        return main(arguments)
    env = dict(os.environ, **{WORKER_ENV: '1'})
    code = run_bounded([sys.executable, '-X', 'utf8', '-B', str(script), *arguments],
                       seconds=seconds, env=env,
                       stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr)
    if code == 124:
        print(f'Time budget exhausted ({seconds:g}s for this diagram); '
              'unfinished work stopped, existing validated files preserved.', file=sys.stderr, flush=True)
    return code

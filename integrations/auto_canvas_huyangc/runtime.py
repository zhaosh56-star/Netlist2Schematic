"""Supervise only adapter-owned processes; no dependency on engine internals."""
from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
from time import monotonic


def run_bounded(command, *, seconds, cancel_event=None, **kwargs):
    options = dict(kwargs)
    if os.name == 'nt':
        options.setdefault('creationflags', getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    else:
        options['start_new_session'] = True
    process = subprocess.Popen(command, **options)

    def stop():
        if os.name == 'nt':
            subprocess.run([str(Path(os.environ['SystemRoot'])/'System32/taskkill.exe'),
                '/PID', str(process.pid), '/T', '/F'], stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if process.poll() is None:
            process.kill()
        process.wait()

    try:
        if cancel_event is None:
            return process.wait(timeout=seconds)
        deadline = monotonic() + seconds
        while True:
            if cancel_event.is_set():
                stop()
                return 130
            remaining = deadline - monotonic()
            if remaining <= 0:
                stop()
                return 124
            try:
                return process.wait(timeout=min(remaining, 0.2))
            except subprocess.TimeoutExpired:
                pass
    except subprocess.TimeoutExpired:
        stop()
        return 124
    except BaseException:
        stop()
        raise

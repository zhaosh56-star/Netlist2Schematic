"""One deadline and cancellation boundary for owned subprocesses."""
from __future__ import annotations

import subprocess
import os
import signal
import threading
import time


class OperationCancelled(RuntimeError):
    pass


class DeadlineExceeded(RuntimeError):
    pass


class RuntimeControl:
    def __init__(self, remaining=None):
        self.remaining = remaining
        self.cancelled = threading.Event()
        self._lock = threading.Lock()
        self._processes = set()

    def check(self):
        if self.cancelled.is_set():
            raise OperationCancelled('operation cancelled')
        if self.remaining and self.remaining() <= 0:
            raise DeadlineExceeded('experiment time budget exhausted')

    def bound(self, timeout):
        self.check()
        return max(.001, min(timeout, self.remaining())) if self.remaining else timeout

    def cancel(self):
        self.cancelled.set()
        with self._lock:
            for process in tuple(self._processes):
                if process.poll() is None:
                    self._stop(process)

    def _stop(self, process):
        if process.poll() is not None:
            return
        try:
            if os.name == 'nt':
                subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2,
                               creationflags=subprocess.CREATE_NO_WINDOW)
            else:
                os.killpg(process.pid, signal.SIGKILL)
        except (OSError, subprocess.TimeoutExpired):
            pass
        if process.poll() is None:
            process.kill()

    def run(self, command, *, timeout, input=None, **kwargs):
        """communicate retries retain input internally; never resend a prompt."""
        self.check()
        started = time.monotonic()
        if os.name == 'nt':
            kwargs.setdefault('creationflags', subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW)
        else:
            kwargs.setdefault('start_new_session', True)
        process = subprocess.Popen(command, stdin=subprocess.PIPE if input is not None else None,
                                   **kwargs)
        with self._lock:
            self._processes.add(process)
        pending = input
        try:
            while True:
                self.check()
                left = timeout - (time.monotonic() - started)
                if left <= 0:
                    raise subprocess.TimeoutExpired(command, timeout)
                wait = min(.2, left, self.bound(timeout))
                try:
                    stdout, stderr = process.communicate(input=pending, timeout=wait)
                    self.check()
                    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
                except subprocess.TimeoutExpired:
                    pending = None
        finally:
            if process.poll() is None:
                self._stop(process)
            process.communicate()
            with self._lock:
                self._processes.discard(process)

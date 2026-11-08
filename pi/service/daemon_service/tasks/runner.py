"""Ticket execution: one detached child per running ticket.

The runner owns processes. A ticket outlives the client that submitted
it, so a child is spawned in its own session and its output goes to the
ticket's log, not to a connection.
"""

import os
import signal
import subprocess
import threading

from .model import CANCELLED, DONE, FAILED, RUNNING, TIMEOUT, Ticket


class TicketRunner:
    """Owns the child processes of tickets and their state changes."""

    def __init__(self, store, config):
        self.store = store
        self.config = config
        self.lock = threading.Lock()
        self.processes = {}
        self.events = {}
        self.cancelled = set()

    def submit(self, session, cwd, command, env=None, timeout=None):
        with self.lock:
            if len(self.processes) >= self.config.max_running_tickets:
                return None
            ticket = Ticket(self.store.alloc(session), session, cwd, command,
                            env, timeout)
            self.store.put(ticket)
            event = threading.Event()
            self.events[ticket.id] = event
            self.cancelled.discard(ticket.id)
            try:
                process = self._spawn(ticket)
            except OSError as exc:
                ticket.finish(FAILED, error=str(exc))
                self.store.put(ticket)
                self.events.pop(ticket.id, None)
                event.set()
                return ticket
            self.processes[ticket.id] = process
            ticket.status = RUNNING
            ticket.pid = process.pid
            ticket.started = ticket.created
            self.store.put(ticket)
        threading.Thread(target=self._drain, args=(ticket, process),
                         daemon=True).start()
        threading.Thread(target=self._wait, args=(ticket, process, event),
                         daemon=True).start()
        return ticket

    def _spawn(self, ticket):
        return subprocess.Popen(
            self._argv(ticket.command),
            cwd=ticket.cwd,
            env=self._environment(ticket),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
            **self._group_kwargs())

    def _argv(self, command):
        if os.name == "nt":
            # Windows runs a command line through its own interpreter; the
            # shell is part of the platform, not a tunable.
            return [os.environ.get("COMSPEC") or "cmd.exe", "/d", "/c",
                    command]
        return [self.config.shell or "/bin/sh", "-lc", command]

    def _environment(self, ticket):
        environment = os.environ.copy()
        environment.update(ticket.env)
        return environment

    @staticmethod
    def _group_kwargs():
        if os.name == "nt":
            return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} \
                if hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP") else {}
        return {"start_new_session": True}

    def _drain(self, ticket, process):
        while True:
            chunk = process.stdout.read(65536)
            if not chunk:
                break
            if self.store.append_output(ticket.id, chunk):
                ticket.truncated = True
        try:
            process.stdout.close()
        except OSError:
            pass

    def _wait(self, ticket, process, event):
        status = DONE
        code = None
        error = None
        try:
            code = process.wait(timeout=ticket.timeout or None)
            if code != 0:
                status = FAILED
        except subprocess.TimeoutExpired:
            self._kill(process)
            code = process.wait()
            status = TIMEOUT
        except OSError as exc:
            status = FAILED
            error = str(exc)
        with self.lock:
            self.processes.pop(ticket.id, None)
            if ticket.id in self.cancelled:
                status = CANCELLED
            self.cancelled.discard(ticket.id)
        ticket.finish(status, code, error)
        self.store.put(ticket)
        event.set()

    def _kill(self, process):
        try:
            if os.name == "nt":
                process.kill()
            else:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass

    def wait(self, tid, timeout):
        with self.lock:
            event = self.events.get(tid)
        if event is not None:
            event.wait(timeout if timeout else None)
        return self.store.get(tid)

    def output(self, tid, offset):
        if self.store.get(tid) is None:
            return None
        return self.store.read_output(tid, offset)

    def cancel(self, tid):
        with self.lock:
            process = self.processes.get(tid)
            if process is not None:
                self.cancelled.add(tid)
        if process is None:
            return self.store.get(tid)
        self._kill(process)
        return self.wait(tid, 5.0) or self.store.get(tid)

    def running(self):
        with self.lock:
            return len(self.processes)

    def stop(self):
        with self.lock:
            processes = list(self.processes.values())
        for process in processes:
            self._kill(process)

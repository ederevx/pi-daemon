"""Ticket execution: one child, one output worker, one completion boundary."""

import os
import signal
import subprocess
import threading
import time

import pi_platform

from ..wire import SPAWN_FAILED, TOO_BUSY
from .error import TaskError
from .model import CANCELLED, DONE, FAILED


class TicketRunner:
    """Completion is child exit plus settled output, then the record."""

    def __init__(self, store, config):
        self._store = store
        self._config = config
        self._lock = threading.Lock()
        self._children = {}
        self._drains = {}
        self._events = {}
        self._timeouts = {}
        self._cancelled = set()
        self._closed = False
        # A ticket command means what it means in the session that offloaded
        # it: the owner resolves the shell its settings chose and passes it
        # as PI_SHELL, and pi_platform resolves bash the same way for this
        # service as for a hosted session.
        self._process = pi_platform.ProcessControl()

    def submit(self, session, cwd, command, timeout=0.0, env=None):
        with self._lock:
            if self._closed:
                raise TaskError(TOO_BUSY)
            snapshot, created = self._store.reserve(
                session, cwd, command, timeout, self._config.max_running)
            ticket_id = snapshot["id"]
            if not created:
                return snapshot, True
            event = threading.Event()
            self._events[ticket_id] = event
            self._timeouts[ticket_id] = snapshot["timeout"]
            try:
                child = self._spawn(snapshot, env)
            except (OSError, ValueError):
                self._store.discard(ticket_id)
                self._events.pop(ticket_id, None)
                self._timeouts.pop(ticket_id, None)
                raise TaskError(SPAWN_FAILED) from None
            self._children[ticket_id] = child
        try:
            self._store.mark_running(ticket_id, child.pid, time.time())
        except LookupError:
            # The reservation was reset away before the run was recorded, so
            # the run never happened and the child goes with it.
            self._terminate(child)
            try:
                child.stdout.close()
            except OSError:
                pass
            self._store.discard(ticket_id)
            self._release(ticket_id)
            raise TaskError(SPAWN_FAILED) from None
        drain = threading.Thread(target=self._drain, name="ticket-output",
                                 args=(ticket_id, child.stdout), daemon=True)
        with self._lock:
            self._drains[ticket_id] = drain
        drain.start()
        threading.Thread(target=self._watch, name="ticket-watch", daemon=True,
                         args=(ticket_id, child)).start()
        return self._store.snapshot_one(ticket_id), False

    def wait(self, ticket_id, timeout=0.0):
        with self._lock:
            event = self._events.get(ticket_id)
        if event is not None and timeout is not None and timeout > 0:
            event.wait(timeout)
        snapshot = self._store.snapshot_one(ticket_id)
        if snapshot is None:
            raise LookupError(f"unknown ticket: {ticket_id}")
        return snapshot

    def cancel(self, ticket_id):
        with self._lock:
            child = self._children.get(ticket_id)
            event = self._events.get(ticket_id)
        if child is None:
            snapshot = self._store.snapshot_one(ticket_id)
            if snapshot is None:
                raise LookupError(f"unknown ticket: {ticket_id}")
            return snapshot
        with self._lock:
            self._cancelled.add(ticket_id)
        self._terminate(child)
        if event is not None:
            event.wait(self._config.cancel_grace)
        return self._store.snapshot_one(ticket_id)

    def begin_shutdown(self):
        with self._lock:
            self._closed = True

    def stop(self):
        self.begin_shutdown()
        with self._lock:
            ticket_ids = list(self._children)
        for ticket_id in ticket_ids:
            with self._lock:
                child = self._children.get(ticket_id)
                event = self._events.get(ticket_id)
                self._cancelled.add(ticket_id)
            if child is not None:
                self._terminate(child)
            if event is not None:
                event.wait(self._config.cancel_grace)
        with self._lock:
            drains = list(self._drains.values())
        for drain in drains:
            drain.join(self._config.stop_grace)

    def _spawn(self, snapshot, env=None):
        cwd = snapshot["cwd"]
        if not os.path.isdir(cwd):
            raise ValueError(f"no such directory: {cwd}")
        # PI_SHELL arrives with an offloaded ticket: the owner resolved the
        # shell its own settings chose, so the ticket runs in that one.
        args = self._process.shell_command(
            snapshot["command"], (env or {}).get("PI_SHELL")
            or self._config.shell or None)
        if os.name == "nt":
            flags = subprocess.CREATE_NEW_PROCESS_GROUP
            session = False
        else:
            flags = 0
            session = True
        environment = dict(os.environ)
        environment.update(env or {})
        return subprocess.Popen(
            args, cwd=cwd, env=environment, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            start_new_session=session, creationflags=flags,
        )

    def _drain(self, ticket_id, pipe):
        try:
            while True:
                chunk = pipe.read(self._config.read_chunk_bytes)
                if not chunk:
                    break
                self._store.append_output(ticket_id, chunk)
        except (OSError, ValueError):
            pass
        finally:
            try:
                pipe.close()
            except OSError:
                pass

    def _watch(self, ticket_id, child):
        outcome, error, exit_code = DONE, "", None
        timeout = self._timeouts.get(ticket_id) or 0.0
        try:
            exit_code = child.wait(timeout=timeout if timeout > 0 else None)
        except subprocess.TimeoutExpired:
            self._terminate(child)
            outcome, error = FAILED, "timed out"
        except OSError as exc:
            outcome, error = FAILED, str(exc)
        drain = self._drains.get(ticket_id)
        if drain is not None:
            drain.join(self._config.drain_grace)
            if drain.is_alive():
                self._store.mark_truncated(ticket_id)
        with self._lock:
            cancelled = ticket_id in self._cancelled
        if cancelled:
            outcome = CANCELLED
        elif outcome == DONE and child.returncode not in (0, None):
            outcome, error = FAILED, f"exit code {child.returncode}"
        try:
            self._store.finish(ticket_id, outcome, child.returncode, error)
        except (LookupError, OSError):
            pass
        finally:
            self._release(ticket_id)

    def _release(self, ticket_id):
        with self._lock:
            self._children.pop(ticket_id, None)
            self._drains.pop(ticket_id, None)
            self._timeouts.pop(ticket_id, None)
            self._cancelled.discard(ticket_id)
            event = self._events.pop(ticket_id, None)
        if event is not None:
            event.set()

    def _terminate(self, child):
        self._signal(child, signal.SIGTERM)
        try:
            child.wait(self._config.cancel_grace)
            return
        except subprocess.TimeoutExpired:
            pass
        except OSError:
            return
        self._signal(child, signal.SIGKILL)

    def _signal(self, child, number):
        try:
            if os.name == "nt":
                child.kill() if number == signal.SIGKILL else child.terminate()
                return
            os.killpg(os.getpgid(child.pid), number)
        except OSError:
            try:
                child.kill()
            except OSError:
                pass

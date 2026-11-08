"""Spawn, supervise and reap hosted sessions; the only owner of a child."""

import os
import threading
import time

from ..wire import SPAWN_FAILED
from .error import SessionError
from .identity import ProcessIdentity


class SessionRunner:
    """A child is recorded before it execs and sealed after it is reaped."""

    def __init__(self, store, relay, config, backend, owner):
        self._store = store
        self._relay = relay
        self._config = config
        self._backend = backend
        self._owner = dict(owner)
        self._lock = threading.RLock()
        self._children = {}
        self._threads = {}
        self._closed = False

    def start(self, name, dir_, argv, cols, rows, env=None, env_once=None):
        """Reserve the name, spawn, record, register with the relay, exec."""
        with self._lock:
            if self._closed:
                raise SessionError(SPAWN_FAILED)
            record, created = self._store.reserve(
                name, dir_, argv, cols, rows, env, self._store.file_of(name),
                argv, self._owner)
            if not created:
                return record.view(), False
            environment = self._environment(name, record, env_once)
        try:
            child = self._backend.spawn(argv, dir_, environment, cols, rows)
        except (OSError, ValueError) as exc:
            self._store.discard(record.id)
            raise SessionError(SPAWN_FAILED) from exc
        try:
            self._store.mark_started(record.id, child.pid,
                                     ProcessIdentity.current(child.pid))
            self._relay.register(record.id, record.generation, child.master)
            child.release()
        except (OSError, LookupError) as exc:
            self._abandon(record.id, child)
            raise SessionError(SPAWN_FAILED) from exc
        watcher = threading.Thread(target=self._watch,
                                   args=(record.id, record.name, child),
                                   name="session-watch", daemon=True)
        with self._lock:
            self._children[record.id] = child
            self._threads[record.id] = watcher
        watcher.start()
        started = self._store.get(record.name)
        return started.view(), True

    def stop(self, name, reason="stop"):
        """Ask one session to end; the watcher seals it."""
        record = self._store.get(name)
        if record is None:
            return {"queued": False, "alive": False, "state": "absent"}
        with self._lock:
            child = self._children.get(record.id)
        if child is None:
            if record.live():
                self._store.finish(record.id, reason, None)
            return {"queued": False, "alive": False, "state": record.state()}
        self._store.request_stop(record.id, reason)
        child.terminate()
        return {"queued": True, "alive": True, "state": "stopping"}

    def send(self, name, data):
        record = self._store.get(name)
        if record is None or not record.live():
            raise SessionError("no-session")
        self._relay.input(record.id, data)
        return len(data)

    def resize(self, name, cols, rows):
        record = self._store.get(name)
        if record is None or not record.live():
            raise SessionError("no-session")
        with self._lock:
            record.cols, record.rows = cols, rows
        self._relay.resize(record.id, cols, rows)

    def running(self, key):
        with self._lock:
            return key in self._children

    def begin_shutdown(self, mode="shutdown"):
        """Stop or preserve children; the mode names the run's ending."""
        reason = "restart" if mode == "reboot" else "shutdown"
        with self._lock:
            children = dict(self._children)
            records = {record.id: record for record in self._store.live()}
        for key, child in children.items():
            record = records.get(key)
            if record is None:
                continue
            self._store.request_stop(key, reason)
            if mode == "reboot":
                record.desired = "running"
                self._store.save()
            child.terminate()

    def shutdown_wait(self):
        with self._lock:
            watchers = list(self._threads.values())
        deadline = time.monotonic() + self._config.stop_grace
        for watcher in watchers:
            remaining = max(0.0, deadline - time.monotonic())
            watcher.join(remaining)
        with self._lock:
            self._closed = True

    def recover(self):
        """A fresh instance owns no child; seal every open run as lost."""
        return self._store.recover(self._owner.get("instance"))

    # -- internals ---------------------------------------------------------

    def _watch(self, key, name, child):
        try:
            returncode = child.wait()
        except Exception:
            returncode = None
        try:
            self._relay.finish(key)
        except Exception:
            pass
        self._store.finish(key, self._reason(key), returncode)
        child.close()
        with self._lock:
            self._children.pop(key, None)
            self._threads.pop(key, None)

    def _reason(self, key):
        recorded = self._store.stop_reason(key)
        if recorded in ("stop", "shutdown", "restart", "purge"):
            return recorded
        record = self._store.get(self._store.name_of(key))
        if record is not None and record.desired == "stopped":
            return "stop"
        return "exit"

    def _abandon(self, key, child):
        try:
            child.terminate(grace=0.2)
        except Exception:
            pass
        child.close()
        self._relay.close(key)
        self._store.discard(key)

    def _environment(self, name, record, env_once):
        environment = dict(os.environ)
        environment.update(record.env)
        environment["PI_DAEMON_SESSION"] = name
        if env_once:
            environment.update(env_once)
        return environment

"""Spawn, supervise and reap hosted sessions; the only owner of a child."""

import os
import signal
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

    def start(self, name, dir_, argv, cols, rows, env=None, env_once=None,
              file=None, durable_argv=None):
        """Reserve the name, spawn, record, register with the relay, exec.

        It returns the reservation itself, not a view: a caller that must
        end or move what it started needs this run's id and generation,
        and re-reading the name could hand it a successor's.
        """
        # The record keeps the durable command only: a one-shot argument
        # (a handover's message) is what this launch carries, not what a
        # later revive may replay.
        durable = list(durable_argv or argv)
        with self._lock:
            if self._closed:
                raise SessionError(SPAWN_FAILED)
            record, created = self._store.reserve(
                name, dir_, durable, cols, rows, env,
                file or self._store.file_of(name), durable, self._owner)
            if not created:
                return record, False
            environment = self._environment(name, record, env_once)
        try:
            child = self._backend.spawn(argv, dir_, environment, cols, rows)
        except (OSError, ValueError) as exc:
            self._store.discard(record.id)
            raise SessionError(SPAWN_FAILED) from exc
        with self._lock:
            self._children[record.id] = child
        try:
            identity = ProcessIdentity.current(child.pid)
            child.identity = identity
            self._store.mark_started(record.id, child.pid, identity)
            master = child.disown_master()
            # The queue runs in order, so a registration that timed out is
            # still ahead of the close that `_abandon` queues behind it;
            # the relay is the only owner that closes the master.
            self._relay.register(record.id, record.generation, master)
            child.release()
        except (OSError, LookupError, TimeoutError) as exc:
            self._abandon(record.id, child)
            raise SessionError(SPAWN_FAILED) from exc
        watcher = threading.Thread(target=self._watch,
                                   args=(record.id, record.name, child),
                                   name="session-watch", daemon=True)
        with self._lock:
            self._threads[record.id] = watcher
            closed = self._closed
        watcher.start()
        if closed:
            # Shutdown started while this child was being recorded, before
            # the snapshot that names its run.
            self._store.request_stop(record.id, "shutdown")
        if closed or record.desired == "stopped":
            # A stop that landed before the start recorded the child.
            child.terminate()
        return record, True

    def stop(self, name, reason="stop"):
        """Ask one session to end; the watcher seals it."""
        record = self._store.get(name)
        if record is None:
            return {"queued": False, "alive": False, "state": "absent"}
        with self._lock:
            child = self._children.get(record.id)
        if child is None:
            if not record.live():
                return {"queued": False, "alive": False,
                        "state": record.state()}
            if record.phase != "spawning":
                self._store.finish(record.id, reason, None)
                return {"queued": False, "alive": False,
                        "state": record.state()}
            # The reservation exists, the process does not yet: the start
            # that owns it terminates what it spawned and is recorded.
            self._store.request_stop(record.id, reason)
            return {"queued": True, "alive": True, "state": "spawning"}
        self._store.request_stop(record.id, reason)
        child.terminate()
        return {"queued": True, "alive": True, "state": "stopping"}

    def stop_run(self, key, generation, reason="stop"):
        """End exactly the run this caller created, never a successor."""
        if not self._store.stop_run(key, generation, reason):
            return False
        with self._lock:
            child = self._children.get(key)
        if child is not None:
            child.terminate()
        return True

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
            self._closed = True
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

    def recover(self):
        """Seal what the last instance left, but only what is really gone."""
        instance = self._owner.get("instance")
        skip, reclaim = [], []
        for record in self._store.live():
            if record.owner.get("instance") == instance:
                continue
            identity = ProcessIdentity.from_dict(record.child.get("identity"))
            pid = record.pid()
            if pid <= 0:
                continue
            verdict = ProcessIdentity.current(pid).classify(identity)
            if verdict == ProcessIdentity.SAME:
                reclaim.append(pid)
            elif verdict == ProcessIdentity.UNKNOWN:
                # The PID cannot be proven gone: leave the run open rather
                # than invent a death the evidence does not support.
                skip.append(record.id)
        for pid in reclaim:
            _end_group(pid, self._config.cancel_grace)
        return self._store.recover(instance, skip=skip)

    # -- internals ---------------------------------------------------------

    def _watch(self, key, name, child):
        try:
            try:
                returncode = child.wait()
            except Exception:
                returncode = None
            try:
                self._relay.finish(key)
            except Exception:
                pass
            self._store.finish(key, self._reason(key), returncode)
        finally:
            try:
                self._relay.close(key)
            except Exception:
                pass
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
        with self._lock:
            self._children.pop(key, None)
            self._threads.pop(key, None)
        try:
            child.terminate(grace=0.2)
        except Exception:
            pass
        self._relay.close(key)
        child.close()
        self._store.discard(key)

    def _environment(self, name, record, env_once):
        # The legacy order: the hosted markers first, then the durable
        # environment a caller passed, then the one-shot additions. A
        # respawn reconstitutes a child from the durable record, so the
        # markers there must already be in place.
        environment = dict(os.environ)
        environment["PI_HOSTED"] = "1"
        environment["PI_HOSTED_SESSION"] = name
        environment.update(record.env)
        if env_once:
            environment.update(env_once)
        return environment


def _end_group(pid, grace):
    """End a group this instance did not father, only while it is identical."""
    identity = ProcessIdentity.current(pid)
    for number in (signal.SIGTERM, signal.SIGKILL):
        try:
            group = os.getpgid(pid)
            if group == os.getpgrp():
                os.kill(pid, number)
            else:
                os.killpg(group, number)
        except OSError:
            return
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            if ProcessIdentity.current(pid).classify(identity) != \
                    ProcessIdentity.SAME:
                return
            time.sleep(0.02)

"""Conversation carry: /new without terminating the source session."""

import hashlib
import os
import time

from ..wire import NAME_TAKEN, NO_FILE, NO_SESSION, SPAWN_FAILED
from .error import SessionError

NO_FREE_NAME = "no-free-name"
# How many random names one carry tries before it gives up and answers
# `no-free-name`; the legacy daemon names a fresh target the same way.
ATTEMPTS = 8


class SessionCarry:
    """It picks or spawns the target, then hands the viewers over.

    The source keeps running headless with its own conversation; only the
    viewers move. A target this call spawned is torn down again when the
    handover cannot happen, and never one it merely selected. /resume of a
    conversation nobody holds hosts it here, under its stopped name when
    it has one.
    """

    def __init__(self, store, runner, relay, command):
        self._store = store
        self._runner = runner
        self._relay = relay
        self._command = command

    def carry(self, name, to=None, argv=None):
        source = self._live(name)
        target, created = self._target(source, to, argv)
        return {"name": self._hand_over(source, target, created).name}

    def resume(self, name, file):
        """/resume without termination: host a conversation nobody holds
        and hand the viewers to it."""
        source = self._live(name)
        target, created = self._host(source, file)
        self._hand_over(source, target, created)
        return {"name": target.name, "state": target.state()}

    # -- internals ---------------------------------------------------------

    def _live(self, name):
        canonical = self._store.resolve(name)
        record = self._store.get(canonical) if canonical else None
        if record is None or not record.live():
            raise SessionError(NO_SESSION)
        return record

    def _target(self, source, to, argv):
        if to is not None:
            target = self._live(to)
            if target.id == source.id:
                raise SessionError(NO_SESSION)
            return target, False
        name = self._free_name(source.dir, source.name)
        if name is None:
            raise SessionError(NO_FREE_NAME)
        return self._spawn(name, source.dir, self._command.fresh(argv), source)

    def _host(self, source, file):
        """The target resuming a conversation no live session holds."""
        if not os.path.isfile(file):
            raise SessionError(NO_FILE)
        name = self._store.stopped_holder(file)
        dir_ = source.dir
        if name is not None:
            recorded = self._store.get(name).dir
            if recorded and os.path.isdir(recorded):
                dir_ = recorded
        else:
            name = self._free_name(source.dir, source.name)
            if name is None:
                raise SessionError(NO_FREE_NAME)
        argv, durable = self._command.resume(file)
        return self._spawn(name, dir_, argv, source, file=file,
                           durable_argv=durable)

    def _spawn(self, name, dir_, argv, source, file=None, durable_argv=None):
        try:
            record, created = self._runner.start(
                name, dir_, argv, source.cols, source.rows, file=file,
                durable_argv=durable_argv)
        except SessionError as failure:
            raise SessionError(failure.code or SPAWN_FAILED) from failure
        if not created:
            # Another spawn won the name between the scan and the reserve.
            raise SessionError(NAME_TAKEN)
        # The reservation itself, not the name: a rollback must end this
        # run and never whatever holds the name by then.
        return record, True

    def _hand_over(self, source, target, created):
        moved = self._relay.carry_viewers(
            source.id, source.generation, target.id, target.generation,
            source.cols, source.rows)
        if moved is None:
            # The source died, the target was replaced, or the target is
            # already closing: no viewer changed hands.
            if created:
                self._runner.stop_run(target.id, target.generation)
            raise SessionError(NO_SESSION)
        return target

    def _free_name(self, dir_, exclude):
        """A unique `pi-<dirbase>-<4hex>`, as the legacy daemon names one."""
        base = os.path.basename(dir_).replace(":", "_")
        if base.startswith("="):
            base = base[1:]
        for attempt in range(ATTEMPTS):
            digest = hashlib.sha256(
                ("%d-%d-%d" % (time.time_ns(), os.getpid(),
                               attempt)).encode()).hexdigest()[:4]
            candidate = "pi-%s-%s" % (base, digest)
            if candidate != exclude and self._store.resolve(candidate) is None:
                return candidate
        return None

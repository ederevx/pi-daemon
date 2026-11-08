"""Conversation carry: /new without terminating the source session."""

import hashlib
import os
import time

from ..wire import NAME_TAKEN, NO_SESSION, SPAWN_FAILED
from .error import SessionError

NO_FREE_NAME = "no-free-name"
ATTEMPTS = 8


class SessionCarry:
    """It picks or spawns the target, then hands the viewers over.

    The source keeps running headless with its own conversation; only the
    viewers move. A target this call spawned is torn down again when the
    handover cannot happen, and never one it merely selected.
    """

    def __init__(self, store, runner, relay, command):
        self._store = store
        self._runner = runner
        self._relay = relay
        self._command = command

    def carry(self, name, to=None, argv=None):
        source = self._live(name)
        target, created = self._target(source, to, argv)
        moved = self._relay.carry_viewers(
            source.id, source.generation, target.id, target.generation,
            source.cols, source.rows)
        if moved is None:
            # The source died, the target was replaced, or the target is
            # already closing: no viewer changed hands.
            if created:
                self._runner.stop_run(target.id, target.generation)
            raise SessionError(NO_SESSION)
        return {"name": target.name}

    # -- internals ---------------------------------------------------------

    def _live(self, name):
        canonical = self._store.resolve(name)
        record = self._store.get(canonical) if canonical else None
        if record is None or not record.live():
            raise SessionError(NO_SESSION)
        return record

    def _target(self, source, to, argv):
        if to:
            target = self._live(to)
            if target.id == source.id:
                raise SessionError(NO_SESSION)
            return target, False
        name = self._free_name(source.dir, source.name)
        if name is None:
            raise SessionError(NO_FREE_NAME)
        try:
            view, created = self._runner.start(
                name, source.dir, self._command.fresh(argv),
                source.cols, source.rows)
        except SessionError as failure:
            raise SessionError(failure.code or SPAWN_FAILED) from failure
        if not created:
            # Another spawn won the name between the scan and the reserve.
            raise SessionError(NAME_TAKEN)
        started = self._store.get(view["name"])
        if started is None:
            raise SessionError(NAME_TAKEN)
        return started, True

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
            existing = self._store.resolve(candidate)
            holder = self._store.get(existing) if existing else None
            if candidate != exclude and (holder is None or not holder.live()):
                return candidate
        return None

"""Hosting a conversation the exiting pi leaves behind."""

import hashlib
import os
import time

from .error import SessionError

# The handing-over pi has just exited: a short settle lets its session
# file finish flushing before the daemon resumes it.
SETTLE = 0.3


class SessionHandover:
    """It names the target and hosts it once the handing-over pi is gone."""

    def __init__(self, store, runner, command, config, settle=SETTLE):
        self._store = store
        self._runner = runner
        self._command = command
        self._config = config
        self._settle = settle

    def target(self, dir_, file):
        """The name this conversation is hosted under, as pi-rc names it."""
        base = os.path.basename(dir_).replace(":", "_")
        if base.startswith("="):
            base = base[1:]
        primary = "pi-" + base
        holder = self._live(primary)
        if holder is None or _holds(holder, dir_):
            return primary
        return "pi-%s-%s" % (base, hashlib.sha256(file.encode()).hexdigest()[:6])

    def taken(self, target, dir_):
        """Another directory's session already holds the target name."""
        holder = self._live(target)
        return holder is not None and not _holds(holder, dir_)

    def host(self, target, dir_, file, message=None):
        """Host the conversation under the promised target, or not at all."""
        time.sleep(self._settle)
        if not os.path.isfile(file):
            return None
        if self._live(target) is not None:
            # Hosted between the request and the exit, or a second
            # handover won the name: hosting twice would diverge.
            return None
        argv, durable = self._command.resume(file, message)
        try:
            record, created = self._runner.start(
                target, dir_, argv, self._config.default_cols,
                self._config.default_rows, file=file, durable_argv=durable)
        except SessionError:
            return None
        return record.name if created else None

    def _live(self, name):
        canonical = self._store.resolve(name)
        record = self._store.get(canonical) if canonical else None
        return record if record is not None and record.live() else None


def _holds(record, dir_):
    try:
        return os.path.realpath(record.dir) == os.path.realpath(dir_)
    except OSError:
        return record.dir == dir_

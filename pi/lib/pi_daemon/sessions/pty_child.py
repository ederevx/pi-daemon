"""One hosted child: its process handle, its PTY and its start gate."""

import errno
import os
import select
import signal
import time

from .identity import ProcessIdentity


class PtyChild:
    """The runner owns process lifetime here; nothing else signals a child."""

    def __init__(self, process, master, gate, error, config):
        self.process = process
        self.master = master
        self._gate = gate
        self._error = error
        self._config = config
        self._released = False
        self.identity = None

    @property
    def pid(self):
        return self.process.pid

    def release(self):
        """Let the helper exec, then confirm the target really started."""
        if self._released:
            return
        self._released = True
        try:
            os.write(self._gate, b"x")
        except OSError:
            pass
        self._close(self._gate)
        self._gate = -1
        failure = self._read_error()
        self._close(self._error)
        self._error = -1
        if failure:
            raise OSError(errno.EIO, failure)

    def _read_error(self):
        deadline = time.monotonic() + self._config.connect_timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                ready, _, _ = select.select([self._error], [], [],
                                            min(remaining, 0.05))
            except (OSError, ValueError):
                return None
            if not ready:
                if self.process.poll() is not None:
                    return self._drain_error()
                continue
            return self._drain_error()

    def _drain_error(self):
        try:
            data = os.read(self._error, 4096)
        except OSError:
            return None
        return data.decode("utf-8", "replace").strip() or None

    def poll(self):
        return self.process.poll()

    def wait(self, timeout=None):
        return self.process.wait(timeout)

    def terminate(self, grace=None):
        """Signal the child's process tree, escalating only after a grace."""
        grace = self._config.cancel_grace if grace is None else grace
        self._signal(signal.SIGTERM)
        try:
            self.process.wait(grace)
            return
        except (OSError, Exception):
            pass
        self._signal(signal.SIGKILL)

    def _signal(self, number):
        """A group is signalled only while it is still this child's."""
        if self.process.returncode is not None:
            return
        if self.identity is not None and ProcessIdentity.current(self.pid) \
                .classify(self.identity) != ProcessIdentity.SAME:
            return
        try:
            if os.name == "nt":
                if number == signal.SIGKILL:
                    self.process.kill()
                else:
                    self.process.terminate()
                return
            group = os.getpgid(self.process.pid)
            if group == os.getpgrp():
                # The helper before its gate opens still shares our group.
                self.process.send_signal(number)
            else:
                os.killpg(group, number)
        except (OSError, ValueError):
            try:
                self.process.send_signal(number)
            except (OSError, ValueError):
                pass

    def disown_master(self):
        """Hand the PTY to the relay; this child never closes it again."""
        master, self.master = self.master, -1
        return master

    def close(self):
        self._close(self.master)
        self.master = -1
        if self._gate > 0:
            self._close(self._gate)
            self._gate = -1
        if self._error > 0:
            self._close(self._error)
            self._error = -1

    @staticmethod
    def _close(descriptor):
        if descriptor is None or descriptor < 0:
            return
        try:
            os.close(descriptor)
        except OSError:
            pass

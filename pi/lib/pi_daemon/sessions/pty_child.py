"""One hosted child: its process handle, its PTY and its start gate."""

import errno
import os
import select
import time

import pi_platform

from .identity import ProcessIdentity


def set_winsize(descriptor, cols, rows):
    """Apply one window size to a PTY descriptor.

    The child owns the live size and the backend applies the same call to
    the slave at spawn, so one owner decides what a size change means.
    """
    import fcntl
    import struct
    import termios

    try:
        fcntl.ioctl(descriptor, termios.TIOCSWINSZ,
                    struct.pack("HHHH", int(rows), int(cols), 0, 0))
    except OSError:
        pass


class PtyChild:
    """The runner owns process lifetime here; the relay owns the I/O."""

    def __init__(self, process, master, gate, error, config):
        self.process = process
        self.master = master
        self._gate = gate
        self._error = error
        self._config = config
        self._control = pi_platform.ProcessControl()
        self._released = False
        self.identity = None

    @property
    def pid(self):
        return self.process.pid

    # -- the runner's side -------------------------------------------------

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
        """End the child's process tree; False when it survived the bound.

        The identity is checked first, so a group is signalled only while
        it is still this child's: a pid the host recycled is never hit.
        """
        grace = self._config.cancel_grace if grace is None else grace
        if self.process.returncode is not None:
            return True
        if self.identity is not None and ProcessIdentity.current(self.pid) \
                .classify(self.identity) != ProcessIdentity.SAME:
            return False
        try:
            self._control.terminate_tree(self.pid, grace,
                                         process=self.process)
        except OSError:
            return False
        return True

    # -- the relay's side --------------------------------------------------

    def output_handle(self):
        return self.master

    def output_valid(self):
        """Whether the master descriptor is still open."""
        try:
            os.fstat(self.master)
        except OSError:
            return False
        return True

    def read_output(self, limit):
        try:
            return os.read(self.master, limit)
        except OSError:
            # EIO is the normal PTY read failure after the child exits.
            return b""

    def write_input(self, data):
        try:
            return os.write(self.master, data)
        except OSError:
            return 0

    def set_winsize(self, cols, rows):
        set_winsize(self.master, cols, rows)

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

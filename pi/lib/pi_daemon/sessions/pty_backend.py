"""Fresh POSIX PTY allocation; the platform seam the runner depends on."""

import os
import struct
import subprocess
import time

# How long a repaint holds the toggled size before restoring it. pi's
# redraw is driven by the change event, and two changes in the same
# instant coalesce into none; the legacy seam uses the same pause.
REPAINT_TICK = 0.05

from .pty_child import PtyChild
from .pty_exec import PtyExec


class PosixPtyBackend:
    """One backend, one platform: a PTY and a child that becomes the target."""

    def __init__(self, config, root):
        self._config = config
        self._root = root

    def spawn(self, argv, cwd, env, cols, rows):
        if os.name == "nt":
            raise OSError("PTY hosting is not available on this platform")
        master, slave = os.openpty()
        self._window(slave, cols, rows)
        gate_read, gate_write = os.pipe()
        error_read, error_write = os.pipe()
        try:
            process = subprocess.Popen(
                PtyExec.helper_argv(self._root, gate_read, error_write,
                                    cwd, list(argv)),
                stdin=slave, stdout=slave, stderr=slave, cwd=cwd, env=env,
                pass_fds=(gate_read, error_write), close_fds=True,
            )
        except BaseException:
            self.close(master)
            for descriptor in (slave, gate_read, gate_write,
                               error_read, error_write):
                self.close(descriptor)
            raise
        for descriptor in (gate_read, slave, error_write):
            self.close(descriptor)
        return PtyChild(process, master, gate_write, error_read,
                        self._config)

    def resize(self, master, cols, rows):
        self._window(master, cols, rows)

    def repaint(self, master, cols, rows):
        """Force a full-frame repaint at the current size.

        A carried viewer sees a cleared screen, and pi redraws only when
        the size really changes, so one row is dropped and restored.
        """
        self._window(master, cols, max(int(rows) - 1, 1) if int(rows) > 1
                     else int(rows))
        time.sleep(REPAINT_TICK)
        self._window(master, cols, rows)

    def close(self, descriptor):
        if descriptor is None or descriptor < 0:
            return
        try:
            os.close(descriptor)
        except OSError:
            pass

    def _window(self, descriptor, cols, rows):
        import fcntl
        import termios

        try:
            fcntl.ioctl(descriptor, termios.TIOCSWINSZ,
                        struct.pack("HHHH", int(rows), int(cols), 0, 0))
        except OSError:
            pass


def service_root():
    """The directory the helper imports its module from."""
    return os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))

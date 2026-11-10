"""Fresh POSIX PTY allocation; the platform seam the runner depends on."""

import os
import subprocess

import pi_platform

from .pty_child import PtyChild, set_winsize
from .pty_exec import PtyExec


class PosixPtyBackend:
    """One backend, one platform: a PTY and a child that becomes the target."""

    def __init__(self, config):
        self._config = config

    def spawn(self, argv, cwd, env, cols, rows):
        if pi_platform.platform_name() == "windows":
            raise OSError("PTY hosting is not available on this platform")
        master, slave = os.openpty()
        set_winsize(slave, cols, rows)
        gate_read, gate_write = os.pipe()
        error_read, error_write = os.pipe()
        try:
            process = subprocess.Popen(
                PtyExec.helper_argv(service_root(), gate_read, error_write,
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

    def close(self, descriptor):
        if descriptor is None or descriptor < 0:
            return
        try:
            os.close(descriptor)
        except OSError:
            pass


def service_root():
    """The directory the helper imports its module from."""
    return os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))

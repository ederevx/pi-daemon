"""The short-lived helper that becomes the hosted process itself."""

import json
import os
import sys

BOOTSTRAP = ("import sys;sys.path.insert(0,{root!r});"
             "from pi_daemon.sessions.pty_exec import PtyExec;"
             "PtyExec.main()")


class PtyExec:
    """It holds the gate until the parent has recorded the child, then execs.

    The helper never outlives the exec: the target process keeps the same
    PID, so the recorded identity is the conversation's identity.
    """

    @classmethod
    def helper_argv(cls, root, gate, error, cwd, argv):
        """The helper inherits the PTY as stdin, stdout and stderr."""
        return [sys.executable, "-I", "-B", "-c", BOOTSTRAP.format(root=root),
                str(gate), str(error), cwd, json.dumps(argv)]

    @classmethod
    def main(cls):
        gate, error, cwd, argv = _arguments()
        if not _gate_open(gate):
            os._exit(1)
        os.close(gate)
        try:
            # An inheritable pipe survives a successful exec, so the parent
            # would wait for an EOF that never comes. Non-inheritable makes
            # the successful exec itself close it.
            os.set_inheritable(error, False)
        except OSError:
            pass
        try:
            _login_tty(0)
            os.chdir(cwd)
            os.execvpe(argv[0], argv, os.environ)
        except BaseException as exc:  # the target never started
            _report(error, f"{argv[0]}: {exc}")
            os._exit(127)
        os._exit(0)


def _arguments():
    raw = sys.argv[1:]
    if len(raw) != 4:
        os._exit(2)
    gate, error, cwd, payload = raw
    return int(gate), int(error), cwd, json.loads(payload)


def _gate_open(gate):
    """The gate closes when the parent has recorded and registered the child."""
    try:
        return os.read(gate, 1) == b"x"
    except OSError:
        return False


def _login_tty(fd):
    try:
        os.login_tty(fd)
        return
    except AttributeError:
        pass
    os.setsid()
    _claim_terminal(fd)
    for number in (1, 2):
        os.dup2(fd, number)


def _claim_terminal(slave):
    import fcntl
    import termios

    try:
        fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
    except OSError:
        pass


def _report(error, message):
    try:
        os.write(error, message.encode("utf-8", "replace")[:4096])
    except OSError:
        pass

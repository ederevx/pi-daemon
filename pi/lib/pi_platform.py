#!/usr/bin/env python3
"""OS-agnostic seams for the pi-daemon PTY host and its pi-rc client.

Every operating-system decision sits behind one small interface with a
POSIX and a Windows implementation:

  - RuntimeLayout    on-disk endpoint/registry/log paths
  - ControlServer /  loopback TCP 127.0.0.1:0 plus a random-token
    ControlClient    hello handshake (the pi-teams teamd pattern),
                     replacing the AF_UNIX socket + chmod(0600)
  - ProcessControl   one terminate/kill-tree path and terminal signalling
  - PtyBackend       POSIX openpty/fork/setsid behind the same interface
                     as the Windows ConPTY backend
  - TerminalMode     raw terminal + resize events for the attach bridge

POSIX-only modules (fcntl, termios, resource) are imported inside the
POSIX implementations, never at module load, and the Windows ConPTY
backend is imported lazily, so importing this module is safe on any
platform.
"""

import json
import os
import signal
import socket
import sys
import time


def send_json(sock, obj):
    """Write one JSON line to a control socket."""
    sock.sendall(json.dumps(obj).encode("utf-8") + b"\n")


def read_json_line(sock):
    """One response line; returns (obj, unconsumed bytes after the line)."""
    buf = b""
    while b"\n" not in buf:
        try:
            data = sock.recv(65536)
        except OSError:
            return None, buf
        if not data:
            return None, buf
        buf += data
    line, rest = buf.split(b"\n", 1)
    try:
        obj = json.loads(line.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        obj = None
    return obj, rest


class RuntimeLayout:
    """Owns every on-disk runtime path, resolved per platform."""

    def __init__(self, environ=None, platform=None):
        self.environ = os.environ if environ is None else environ
        self.platform = sys.platform if platform is None else platform

    def is_windows(self):
        return self.platform.startswith("win")

    def runtime_dir(self):
        explicit = self.environ.get("XDG_RUNTIME_DIR")
        if explicit:
            return explicit
        if self.is_windows():
            base = (self.environ.get("TEMP") or self.environ.get("TMP")
                    or os.path.expanduser("~"))
            return os.path.join(base, "pi-daemon")
        uid = os.getuid() if hasattr(os, "getuid") else 0
        return "/run/user/%d" % uid

    def state_home(self):
        explicit = self.environ.get("XDG_STATE_HOME")
        if explicit:
            return explicit
        if self.is_windows():
            return (self.environ.get("LOCALAPPDATA")
                    or os.path.expanduser("~"))
        return os.path.expanduser("~/.local/state")

    def registry_dir(self):
        return os.path.join(self.state_home(), "pi-pty-host")

    def endpoint_path(self):
        return os.path.join(self.runtime_dir(), "pi-pty-host.sock")

    def registry_path(self):
        return os.path.join(self.registry_dir(), "sessions.json")

    def tickets_path(self):
        return os.path.join(self.registry_dir(), "tickets.json")

    def ticket_log_dir(self):
        return os.path.join(self.registry_dir(), "tickets")

    def daemon_log_path(self):
        return os.path.join(self.registry_dir(), "daemon.log")

    def ext_snapshot_path(self):
        return os.path.join(self.registry_dir(),
                            "extensions-snapshot.json")

    def ext_diff_path(self):
        return os.path.join(self.registry_dir(), "extensions-diff.json")


class EndpointFile:
    """Atomic read/write of the published {host, port, token} endpoint."""

    def __init__(self, path):
        self.path = path

    def write(self, host, port, token):
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        data = json.dumps({"host": host, "port": port,
                           "token": token}) + "\n"
        tmp = "%s.tmp.%d" % (self.path, os.getpid())
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(data)
        self._restrict(tmp)
        os.replace(tmp, self.path)
        self._restrict(self.path)

    def _restrict(self, path):
        # On POSIX this keeps the token out of other users' reach; on
        # Windows chmod only toggles the read-only bit, which is fine
        # because the endpoint lives under %LOCALAPPDATA%.
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    def read(self):
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def remove(self):
        try:
            os.remove(self.path)
        except OSError:
            pass


class ControlHandshake:
    """Validates the mandatory hello line of a control connection."""

    def __init__(self, token):
        self.token = token

    def validate(self, msg):
        if not isinstance(msg, dict):
            return False
        return msg.get("cmd") == "hello" and msg.get("token") == self.token

    def ack(self):
        return {"ok": True, "srv": "pi-daemon"}

    def reject(self):
        return {"ok": False, "error": "bad-handshake"}


class ControlServer:
    """Loopback TCP listener that publishes its ephemeral port + token."""

    def __init__(self, endpoint, host="127.0.0.1"):
        self.endpoint = endpoint
        self.host = host
        self.sock = None
        self.port = None

    def bind(self, token):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            srv.bind((self.host, 0))
            srv.listen(16)
        except OSError:
            srv.close()
            raise
        self.sock = srv
        self.port = srv.getsockname()[1]
        self.endpoint.write(self.host, self.port, token)
        return self

    def accept(self):
        return self.sock.accept()

    def close(self):
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None


class ControlClient:
    """Connects to a published endpoint and performs the token handshake."""

    def __init__(self, endpoint):
        self.endpoint = endpoint

    def connect(self, timeout):
        data = self.endpoint.read()
        host = data.get("host")
        port = data.get("port")
        token = data.get("token") or ""
        if not host or not port:
            raise OSError("no endpoint published")
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            sock.connect((host, port))
            self._handshake(sock, token)
        except OSError:
            sock.close()
            raise
        sock.settimeout(None)
        return sock

    def _handshake(self, sock, token):
        handshake = ControlHandshake(token)
        send_json(sock, {"cmd": "hello", "token": handshake.token})
        reply, _ = read_json_line(sock)
        if not isinstance(reply, dict) or not reply.get("ok"):
            raise OSError("handshake rejected")


class ProcessControl:
    """One portable terminate/kill-tree path and terminal signalling."""

    def __init__(self, platform=None):
        self.platform = sys.platform if platform is None else platform

    def terminate_tree(self, pid, grace):
        if self.platform.startswith("win"):
            self._windows_terminate(pid)
        else:
            self._posix_terminate(pid, grace)

    def signal_winch(self, pid):
        if self.platform.startswith("win"):
            return
        try:
            os.killpg(pid, signal.SIGWINCH)
        except OSError:
            pass

    def signal_term(self, pid):
        if self.platform.startswith("win"):
            self._windows_terminate(pid)
            return
        try:
            os.killpg(pid, signal.SIGTERM)
        except OSError:
            pass

    def signal_kill(self, pid):
        if self.platform.startswith("win"):
            self._windows_terminate(pid)
            return
        try:
            os.killpg(pid, signal.SIGKILL)
        except OSError:
            pass

    def _posix_terminate(self, pid, grace):
        try:
            os.killpg(pid, signal.SIGTERM)
        except OSError:
            pass
        deadline = time.time() + grace
        while time.time() < deadline:
            try:
                if os.waitpid(pid, os.WNOHANG)[0] == pid:
                    return
            except ChildProcessError:
                return
            time.sleep(0.1)
        try:
            os.killpg(pid, signal.SIGKILL)
        except OSError:
            pass

    def windowless_python(self, interpreter=None):
        """Map an interpreter to its GUI-subsystem twin on Windows.

        A detached daemon must not flash a console, so python.exe becomes
        pythonw.exe (and py.exe becomes pyw.exe). Off Windows, and when
        no twin exists, the interpreter is returned unchanged.
        """
        interp = interpreter or sys.executable or "python3"
        if not self.platform.startswith("win"):
            return interp
        import shutil
        lower = interp.lower()
        candidates = []
        if lower.endswith("python.exe"):
            candidates.append(interp[: -len("python.exe")] + "pythonw.exe")
        elif lower.endswith("python3.exe"):
            candidates.append(interp[: -len("python3.exe")] + "pythonw.exe")
        if lower in ("py", "py.exe"):
            candidates.append("pyw")
        candidates.append("pythonw")
        for candidate in candidates:
            if os.path.isabs(candidate):
                if os.path.isfile(candidate):
                    return candidate
            elif shutil.which(candidate):
                return candidate
        return interp

    def launch_detached(self, argv, logf):
        """Start a long-lived child that outlives this process."""
        import subprocess
        kwargs = {"stdin": subprocess.DEVNULL, "stdout": logf,
                  "stderr": subprocess.STDOUT}
        if self.platform.startswith("win"):
            kwargs["creationflags"] = (
                getattr(subprocess, "CREATE_NO_WINDOW", 0)
                | getattr(subprocess, "DETACHED_PROCESS", 0))
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen(argv, **kwargs)

    def wait_for_parent_exit(self, pid):
        """Block until the process that started us has exited.

        POSIX: the handing-over pi starts pi-rc as its child, so a parent
        id change (reparenting) is the exit signal. Windows: wait on the
        parent process handle. Neither path probes with a signal.
        """
        if self.platform.startswith("win"):
            self._windows_wait_exit(pid)
            return
        while os.getppid() == pid:
            time.sleep(0.2)

    def _windows_wait_exit(self, pid):
        import ctypes
        SYNCHRONIZE = 0x00100000
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(SYNCHRONIZE, False, int(pid))
        if not handle:
            return
        try:
            kernel32.WaitForSingleObject(handle, 0xFFFFFFFF)
        finally:
            kernel32.CloseHandle(handle)

    def _windows_terminate(self, pid):
        import subprocess
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL,
                           creationflags=getattr(subprocess,
                                                  "CREATE_NO_WINDOW", 0),
                           timeout=5)
        except (OSError, subprocess.SubprocessError):
            pass


class PtyChild:
    """One child on a pseudo-terminal, owned by a PtyBackend."""

    def output_handle(self):
        raise NotImplementedError

    def read_output(self, limit):
        raise NotImplementedError

    def write_input(self, data):
        raise NotImplementedError

    def set_winsize(self, cols, rows):
        raise NotImplementedError

    def get_winsize(self):
        return (0, 0)

    def signal_winch(self):
        pass

    def wait_nohang(self):
        raise NotImplementedError

    def kill_tree(self, grace):
        raise NotImplementedError

    def close(self):
        pass


class PosixPtyChild(PtyChild):
    """POSIX openpty master: direct fd I/O plus waitpid reap."""

    def __init__(self, pid, master_fd, control=None):
        self.pid = pid
        self.master_fd = master_fd
        self.control = control if control is not None else ProcessControl()

    def output_handle(self):
        return self.master_fd

    def read_output(self, limit):
        try:
            return os.read(self.master_fd, limit)
        except OSError:
            # EIO is the normal PTY read failure after child exit.
            return b""

    def write_input(self, data):
        try:
            return os.write(self.master_fd, data)
        except OSError:
            return 0

    def set_winsize(self, cols, rows):
        import fcntl
        import struct
        import termios
        try:
            fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ,
                        struct.pack("HHHH", rows, cols, 0, 0))
        except OSError:
            pass

    def get_winsize(self):
        import fcntl
        import struct
        import termios
        try:
            pack = fcntl.ioctl(self.master_fd, termios.TIOCGWINSZ,
                               struct.pack("HHHH", 0, 0, 0, 0))
            rows, cols = struct.unpack("HHHH", pack)[:2]
        except OSError:
            rows, cols = 0, 0
        return (rows, cols)

    def signal_winch(self):
        self.control.signal_winch(self.pid)

    def wait_nohang(self):
        try:
            reaped = os.waitpid(self.pid, os.WNOHANG)
        except ChildProcessError:
            return (True, 0)
        if reaped[0] == self.pid:
            return (True, reaped[1])
        return (False, 0)

    def kill_tree(self, grace):
        self.control.terminate_tree(self.pid, grace)

    def close(self):
        try:
            os.close(self.master_fd)
        except OSError:
            pass


class PosixPtyBackend:
    """openpty + fork + setsid/TIOCSCTTY behind the PtyBackend interface."""

    name = "posix"

    def __init__(self, control=None):
        self.control = control if control is not None else ProcessControl()

    def spawn(self, argv, cwd, env, cols, rows):
        import fcntl
        import termios
        master, slave = os.openpty()
        try:
            fcntl.ioctl(slave, termios.TIOCSWINSZ,
                        _winsize_pack(cols, rows))
        except OSError:
            pass
        pid = os.fork()
        if pid == 0:
            self._exec_child(argv, cwd, env, slave, cols, rows)
        os.close(slave)
        return PosixPtyChild(pid, master, self.control)

    def _exec_child(self, argv, cwd, env, slave, cols, rows):
        import fcntl
        import resource
        import termios
        try:
            os.setsid()
            fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
            os.dup2(slave, 0)
            os.dup2(slave, 1)
            os.dup2(slave, 2)
            os.chdir(cwd)
            maxfd = resource.getrlimit(resource.RLIMIT_NOFILE)[0]
            if maxfd == resource.RLIM_INFINITY or maxfd > 65536:
                maxfd = 65536
            os.closerange(3, maxfd)
            os.execvpe(argv[0], argv, env)
        except Exception as exc:
            try:
                sys.stderr.write("pi-daemon: exec failed: %s\n" % (exc,))
            except Exception:
                pass
            os._exit(127)


def _winsize_pack(cols, rows):
    import struct
    return struct.pack("HHHH", rows, cols, 0, 0)


class TerminalMode:
    """Raw terminal + resize-event seam for the pi-rc attach bridge."""

    def enter(self):
        raise NotImplementedError

    def restore(self):
        pass

    def size(self):
        raise NotImplementedError


class PosixTerminal(TerminalMode):
    """POSIX raw mode over fd 0 plus a self-pipe for SIGWINCH."""

    def __init__(self, fd=0):
        self.fd = fd
        self.saved = None

    def enter(self):
        import termios
        attrs = termios.tcgetattr(self.fd)
        self.saved = attrs
        attrs[0] &= ~(termios.BRKINT | termios.ICRNL | termios.INPCK
                      | termios.ISTRIP | termios.IXON)
        attrs[1] &= ~termios.OPOST
        attrs[3] &= ~(termios.ICANON | termios.ECHO | termios.ISIG
                      | termios.IEXTEN)
        attrs[6][termios.VMIN] = 1
        attrs[6][termios.VTIME] = 0
        termios.tcsetattr(self.fd, termios.TCSANOW, attrs)

    def restore(self):
        if self.saved is None:
            return
        import termios
        try:
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)
        except termios.error:
            pass
        self.saved = None

    def size(self):
        try:
            sz = os.get_terminal_size(self.fd)
            return (sz.columns, sz.lines)
        except OSError:
            return (0, 0)


def select_pty_backend(platform=None):
    """The PTY backend for this platform: POSIX, or lazy ConPTY."""
    platform = sys.platform if platform is None else platform
    if platform.startswith("win"):
        from pi_conpty import WindowsPtyBackend
        return WindowsPtyBackend()
    return PosixPtyBackend()


def select_terminal_mode(platform=None):
    """The attach terminal seam for this platform."""
    platform = sys.platform if platform is None else platform
    if platform.startswith("win"):
        from pi_conpty import WindowsConsole
        return WindowsConsole()
    return PosixTerminal()
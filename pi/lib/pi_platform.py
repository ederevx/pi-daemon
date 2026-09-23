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

# A repaint nudge waits this long between toggling the PTY size and
# restoring it, so the child processes the intermediate size change
# before the real one.
_REPAINT_TICK = 0.05


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

    def _join(self, *parts):
        # Path syntax follows the declared platform, not the host, so the
        # seam stays honest when a platform is injected for tests.
        import ntpath
        import posixpath
        module = ntpath if self.is_windows() else posixpath
        return module.join(*parts)

    def runtime_dir(self):
        explicit = self.environ.get("XDG_RUNTIME_DIR")
        if explicit:
            return explicit
        if self.is_windows():
            base = (self.environ.get("TEMP") or self.environ.get("TMP")
                    or os.path.expanduser("~"))
            return self._join(base, "pi-daemon")
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

    def agent_home(self):
        # The pi agent home: PI_CODING_AGENT_DIR wins, matching install.sh
        # and the pi runtime, else the conventional ~/.pi/agent.
        explicit = self.environ.get("PI_CODING_AGENT_DIR")
        if explicit:
            return explicit
        return os.path.expanduser("~/.pi/agent")

    def registry_dir(self):
        return self._join(self.state_home(), "pi-pty-host")

    def endpoint_path(self):
        return self._join(self.runtime_dir(), "pi-pty-host.sock")

    def registry_path(self):
        return self._join(self.registry_dir(), "sessions.json")

    def tickets_path(self):
        return self._join(self.registry_dir(), "tickets.json")

    def ticket_log_dir(self):
        return self._join(self.registry_dir(), "tickets")

    def daemon_log_path(self):
        return self._join(self.registry_dir(), "daemon.log")

    def ext_snapshot_path(self):
        return self._join(self.registry_dir(),
                          "extensions-snapshot.json")

    def ext_diff_path(self):
        return self._join(self.registry_dir(), "extensions-diff.json")


class AtomicStateFile:
    """Atomic JSON state persistence: one owner for the write-temp-fsync-
    replace pattern every durable state file uses. The temp name carries
    the pid so two concurrent processes converging on one state dir
    (roster boot converge, detached successor races) can never corrupt
    each other's in-flight temp; os.replace publishes readers a complete
    file or nothing, on every platform. "restrict" keeps a written file
    out of other users' reach on POSIX. read() returns the parsed JSON
    or None for a missing/corrupt file, so every state store reads
    through the same guarded path."""

    def __init__(self, path, restrict=False):
        self.path = path
        self.restrict = restrict

    def write(self, obj):
        """Serialize obj as one JSON line and publish it atomically:
        write to a pid-unique temp in the target directory, flush, fsync
        (the file survives a crash, not just a clean exit), then
        os.replace. Raises OSError on failure; callers keep their own
        failure semantics."""
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp = "%s.tmp.%d" % (self.path, os.getpid())
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps(obj) + "\n")
            f.flush()
            os.fsync(f.fileno())
        if self.restrict:
            self._restrict(tmp)
        os.replace(tmp, self.path)
        if self.restrict:
            self._restrict(self.path)

    def read(self):
        """The parsed file, or None when it is missing or corrupt."""
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return None

    def read_dict(self):
        """The parsed file when it holds an object, else {}: the guarded
        read every dict-shaped state store uses on load."""
        data = self.read()
        return data if isinstance(data, dict) else {}

    def _restrict(self, path):
        # On POSIX this keeps the state out of other users' reach; on
        # Windows chmod only toggles the read-only bit, which is fine
        # because the state lives under %LOCALAPPDATA%.
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


class EndpointFile:
    """Atomic read/write of the published {host, port, token} endpoint."""

    def __init__(self, path):
        self.path = path
        self._file = AtomicStateFile(path, restrict=True)

    def write(self, host, port, token):
        # AtomicStateFile.write creates the target directory itself.
        self._file.write({"host": host, "port": port, "token": token})

    def read(self):
        return self._file.read_dict()

    def remove(self):
        try:
            os.remove(self.path)
        except OSError:
            pass

    def remove_if(self, host, port, token):
        """Unlink the endpoint file only while it still publishes our
        own coordinates: a dying daemon must never delete the file a
        successor has already published over it."""
        data = self.read()
        if (data.get("host") == host and data.get("port") == port
                and data.get("token") == token):
            self.remove()


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
        if sys.platform == "win32":
            # Windows SO_REUSEADDR permits double binds, which let a stale
            # probe steal the endpoint; claim the port exclusively instead.
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
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
    """One portable terminate/kill-tree path, terminal signalling, and
    pid liveness."""

    def __init__(self, platform=None, environ=None):
        self.platform = sys.platform if platform is None else platform
        self.environ = os.environ if environ is None else environ

    def pid_alive(self, pid):
        """True while a pid still exists as a live process; a reaped or
        zombie process counts as gone. Best effort: without os.kill
        every pid is presumed alive."""
        if self.platform.startswith("win") or not hasattr(os, "kill"):
            return True
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        try:
            with open("/proc/%d/stat" % pid) as fh:
                return not fh.read().rsplit(")", 1)[1].lstrip().startswith("Z")
        except OSError:
            return True

    def process_start_token(self, pid):
        """A token identifying this exact process incarnation: the
        starttime field (ticks since boot) of /proc/<pid>/stat. A bare
        signal probe cannot tell a recycled pid from the original one;
        comparing recorded tokens can. Returns None where the start
        time is unavailable (Windows has no /proc)."""
        if self.platform.startswith("win"):
            return None
        try:
            with open("/proc/%d/stat" % pid) as fh:
                # Drop "pid (comm) "; the remaining fields start at 3,
                # so overall field 22 (starttime) is index 19.
                return fh.read().rsplit(")", 1)[1].split()[19]
        except (OSError, IndexError):
            return None

    def pid_matches_token(self, pid, token):
        """True while pid is still the process incarnation the token was
        recorded for. A token that was never recorded (a pre-token
        roster entry, or no /proc on Windows) cannot be disproved, so
        it always matches."""
        current = self.process_start_token(pid)
        if current is None or token is None:
            return True
        return str(current) == str(token)

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

    def resume_command(self):
        """argv that resumes the latest pi conversation, else starts one.

        One shell path on every platform: bash runs `pi -c` and `exec`
        replaces it with the interactive pi, so the PTY hosts pi itself.
        """
        return self.shell_command("pi -c || exec pi")

    def shell_path(self, explicit=None):
        """A bash path usable on every platform, resolved once.

        One candidate order serves every platform: an explicit path (pi
        hands over the shell its settings chose, delivered as PI_SHELL),
        then bash on PATH, then the conventional install locations
        (`%ProgramFiles%\\Git\\bin\\bash.exe` and `/bin/bash`), so no
        platform owns a separate code path. Empty when none exists.

        A candidate that is the WSL launcher (`bash.exe` in the Windows
        system directory) is rejected: it runs the command inside the
        default Linux distro, where the Windows-hosted pi chain does not
        exist, so any session started through it dies with 127. This is
        the only bash a native-Windows PATH resolves, so it must fall
        through to the next candidate instead.
        """
        import shutil
        candidates = [explicit, self.environ.get("PI_SHELL"),
                      shutil.which("bash"), shutil.which("bash.exe")]
        for base in (self.environ.get("ProgramFiles"),
                     self.environ.get("ProgramFiles(x86)")):
            if base:
                candidates.append(os.path.join(base, "Git", "bin", "bash.exe"))
        candidates.append("/bin/bash")
        for candidate in candidates:
            if candidate and os.path.isfile(candidate) \
                    and not self._is_wsl_stub(candidate):
                return candidate
        return ""

    def _is_wsl_stub(self, path):
        """Whether a bash candidate is the WSL launcher stub.

        Windows ships `bash.exe` beside cmd.exe in the system directory
        as the WSL entry point; it execs into the default Linux distro.
        A hosted session needs a Windows-side bash whose children can
        reach the Windows pi wrapper, npm shim, and node, so the stub is
        never an acceptable resolution and the next candidate wins.
        Applies to every candidate including an explicit override: the
        stub cannot host a Windows session at all, so honoring it would
        only guarantee a 127.
        """
        root = self._env_value("SystemRoot", "windir")
        if not root:
            return False
        probe = os.path.normcase(os.path.abspath(path))
        for sub in ("System32", "Sysnative"):
            stub = os.path.normcase(os.path.abspath(
                os.path.join(root, sub, "bash.exe")))
            if probe == stub:
                return True
        return False

    def _env_value(self, *names):
        """First environ value matching any name, case-insensitively.

        os.environ is a case-insensitive mapping, but a copied plain
        dict (as the daemon passes) is not, and the block's casing is
        not under our control.
        """
        lowered = {name.lower() for name in names}
        for key, value in self.environ.items():
            if key.lower() in lowered:
                return value
        return None

    def pi_argv(self, args=()):
        """argv that launches the bundled `pi` with `args` on every
        platform.

        `pi` is a shell shim, not a program a Windows CreateProcess can
        resolve by name, so start it through the shell seam: the shell
        finds `pi`, `exec` replaces itself with it, and `"$@"` passes the
        arguments verbatim (a Windows path is never mangled).
        """
        shell = self.shell_path() or "sh"
        return [shell, "-c", 'exec pi "$@"', "pi", *args]

    def shell_command(self, command, shell=None):
        """argv that runs one command string in bash on every platform.

        Bash is resolved the same way everywhere, so an offloaded command
        runs in the shell pi's local bash tool would have used. The bare
        `sh` name is the single fallback, matching pi's own last resort.
        """
        return [self.shell_path(shell) or "sh", "-c", command]

    def group_kwargs(self):
        """subprocess kwargs for a detached command child.

        POSIX gets its own session so a killpg reaps the tree; Windows
        gets CREATE_NO_WINDOW so a console child (cmd or bash) never
        opens a visible console window, mirroring pi-teams' windowsHide
        on every spawn.
        """
        if self.platform.startswith("win"):
            import subprocess
            return {"creationflags":
                    getattr(subprocess, "CREATE_NO_WINDOW", 0)}
        return {"start_new_session": True}

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
        self.taskkill_tree(pid)

    def taskkill_tree(self, pid, timeout=5):
        """Windows tree kill: one taskkill /T /F owner for the daemon's
        signal paths and the ConPTY backend's child teardown."""
        import subprocess
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL,
                           creationflags=getattr(subprocess,
                                                  "CREATE_NO_WINDOW", 0),
                           timeout=timeout)
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
        """Ask the child to repaint.

        A TUI redraws only when its winsize really changes, so an
        implementation must force one even when the requested size
        equals the current size: POSIX toggles the PTY size around
        SIGWINCH, Windows toggles the pseudoconsole size.
        """

    def exit_abnormal(self, status):
        """Whether a reaped status is an abnormal death.

        The child owns its exit-status encoding: a POSIX waitpid status
        versus a raw Windows exit code. A nonzero code is abnormal on
        both; PosixPtyChild refines this with the wait-status macros so
        signal deaths stay distinct from clean quits.
        """
        return bool(status)

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
        # A bare SIGWINCH is not enough: Node emits the tty resize event
        # that drives pi's repaint only when the winsize really changes,
        # so a resize or attach at the current size would stay silent.
        # Toggle one row, pause, then restore the real size, so the child
        # always sees a change and redraws its whole frame. The Windows
        # child does the same through the pseudoconsole, so the daemon
        # carries no OS branch for the repaint.
        rows, cols = self.get_winsize()
        if rows <= 0 or cols <= 0:
            # The winsize is unknown (ioctl failed): nothing to toggle.
            self.control.signal_winch(self.pid)
            return
        if rows > 1:
            self.set_winsize(cols, rows - 1)
        else:
            self.set_winsize(max(cols - 1, 1), rows)
        self.control.signal_winch(self.pid)
        time.sleep(_REPAINT_TICK)
        self.set_winsize(cols, rows)
        self.control.signal_winch(self.pid)

    def exit_abnormal(self, status):
        if status and os.WIFSIGNALED(status):
            return True
        if status and os.WIFEXITED(status):
            return os.WEXITSTATUS(status) != 0
        return False

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
    """Raw terminal + resize seam for the pi-rc attach bridge.

    The bridge selects on `input_fd()` (and `wake_fd()` when the platform
    needs a separate wakeup) and writes through `write_output()`. A
    platform whose output fd is not select-able (Windows console handles)
    returns None from `writable_fd()`, and the bridge writes directly.
    Resize is a flag (`take_resize()`), backed on POSIX by SIGWINCH and on
    Windows by polling the console window size.
    """

    def enter(self):
        raise NotImplementedError

    def restore(self):
        pass

    def size(self):
        raise NotImplementedError

    def input_fd(self):
        raise NotImplementedError

    def wake_fd(self):
        return None

    def writable_fd(self):
        return None

    def read_input(self, limit):
        raise NotImplementedError

    def write_output(self, data):
        raise NotImplementedError

    def take_resize(self):
        return False

    def close(self):
        self.restore()


class PosixTerminal(TerminalMode):
    """POSIX raw mode over fd 0 plus a self-pipe for SIGWINCH."""

    def __init__(self, fd=0, out_fd=1):
        self.fd = fd
        self.out_fd = out_fd
        self.saved = None
        self.saved_blocking = None
        self.saved_out_blocking = None
        self.wake_read = None
        self.wake_write = None
        self.resized = False
        self.prev_winch = None
        self.winch_installed = False

    def enter(self):
        self._make_wake_pipe()
        self._install_winch()
        import termios
        self.saved = termios.tcgetattr(self.fd)
        self.saved_blocking = os.get_blocking(self.fd)
        self.saved_out_blocking = os.get_blocking(self.out_fd)
        os.set_blocking(self.fd, False)
        os.set_blocking(self.out_fd, False)
        self._raw_mode()

    def _make_wake_pipe(self):
        self.wake_read, self.wake_write = os.pipe()
        os.set_blocking(self.wake_read, False)
        os.set_blocking(self.wake_write, False)

    def _install_winch(self):
        if not hasattr(signal, "SIGWINCH"):
            return
        self.prev_winch = signal.getsignal(signal.SIGWINCH)
        signal.signal(signal.SIGWINCH, self._on_winch)
        self.winch_installed = True

    def _on_winch(self, _signum, _frame):
        # Signal handlers must stay tiny: flag the resize and wake the
        # select loop; all daemon/terminal work happens on the main loop.
        self.resized = True
        try:
            os.write(self.wake_write, b"x")
        except OSError:
            pass

    def _raw_mode(self):
        import termios
        attrs = termios.tcgetattr(self.fd)
        attrs[0] &= ~(termios.BRKINT | termios.ICRNL | termios.INPCK
                      | termios.ISTRIP | termios.IXON)
        attrs[1] &= ~termios.OPOST
        attrs[3] &= ~(termios.ICANON | termios.ECHO | termios.ISIG
                      | termios.IEXTEN)
        attrs[6][termios.VMIN] = 1
        attrs[6][termios.VTIME] = 0
        termios.tcsetattr(self.fd, termios.TCSANOW, attrs)

    def restore(self):
        if self.saved is not None:
            import termios
            try:
                termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)
            except termios.error:
                pass
            self.saved = None
        if self.saved_blocking is not None:
            try:
                os.set_blocking(self.fd, self.saved_blocking)
            except OSError:
                pass
            self.saved_blocking = None
        if self.saved_out_blocking is not None:
            try:
                os.set_blocking(self.out_fd, self.saved_out_blocking)
            except OSError:
                pass
            self.saved_out_blocking = None
        if self.winch_installed:
            signal.signal(signal.SIGWINCH, self.prev_winch)
            self.winch_installed = False
        self._close_wake_pipe()

    def _close_wake_pipe(self):
        for name in ("wake_read", "wake_write"):
            fd = getattr(self, name)
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
                setattr(self, name, None)

    def size(self):
        try:
            sz = os.get_terminal_size(self.fd)
            return (sz.columns, sz.lines)
        except OSError:
            return (0, 0)

    def input_fd(self):
        return self.fd

    def wake_fd(self):
        return self.wake_read

    def writable_fd(self):
        return self.out_fd

    def read_input(self, limit):
        try:
            return os.read(self.fd, limit)
        except (BlockingIOError, InterruptedError):
            return None
        except OSError:
            return b""

    def write_output(self, data):
        try:
            return os.write(self.out_fd, data)
        except (BlockingIOError, InterruptedError):
            return 0
        except OSError:
            return None

    def take_resize(self):
        self._drain_wake_pipe()
        if self.resized:
            self.resized = False
            return True
        return False

    def _drain_wake_pipe(self):
        if self.wake_read is None:
            return
        try:
            while os.read(self.wake_read, 4096):
                pass
        except (BlockingIOError, InterruptedError, OSError):
            pass


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
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
import subprocess
import sys
import threading
import time

# Win32 facts a process probe needs: the access right that lets a handle
# be queried without the right to end the process, the exit code a
# running process reports, and the error a denied OpenProcess raises.
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259
_ERROR_ACCESS_DENIED = 5


# -- private files ---------------------------------------------------------

# The two well-known identities every Windows file already carries and no
# owner can remove: the operating system itself and the local administrators.
# OWNER RIGHTS (S-1-3-4) belongs here too: an entry carrying it applies to
# the owner and to nobody else, which is what the file already promises.
_PRIVATE_SIDS = ("S-1-5-18", "S-1-5-32-544", "S-1-3-4")


def open_private_file(path):
    """Open a private file once, and say why it is not one.

    A credential is one regular file only its owner may read. POSIX says
    that with the inode's owner and mode bits; Windows keeps no such bits,
    so its access control list is read instead - the file must be owned by
    this user, and every entry that grants access must name this user,
    SYSTEM, or Administrators - and a reparse point is refused, because
    such a path opens something other than the file the check looked at.

    The check and the handle belong to one object: the descriptor returned
    is the file that was cleared, so a reader never opens the path a second
    time and has no inode to compare. Returns (problem, descriptor, info)
    with exactly one of problem and descriptor set.
    """
    if not isinstance(path, str) or not os.path.isabs(path):
        return ("an absolute path is required", None, None)
    if os.name == "nt":
        return _windows_open_private(path)
    return _posix_open_private(path)


def private_file_problem(path):
    """Why this file is not a private credential, and its stat."""
    problem, descriptor, info = open_private_file(path)
    if descriptor is not None:
        os.close(descriptor)
    return (problem, info)


def _posix_open_private(path):
    import stat

    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as failure:
        return ("the file is unreadable: %s" % failure, None, None)
    try:
        info = os.fstat(descriptor)
    except OSError as failure:
        os.close(descriptor)
        return ("the file is unreadable: %s" % failure, None, None)
    problem = None
    if not stat.S_ISREG(info.st_mode):
        problem = "a regular file is required"
    elif info.st_uid != os.geteuid():
        problem = "a file of mine is required"
    elif info.st_mode & 0o077:
        problem = "the file is readable by others"
    if problem is not None:
        os.close(descriptor)
        return (problem, None, info)
    return (None, descriptor, info)


def _windows_open_private(path):
    """One CreateFileW, then the DACL of that handle, then read it."""
    import ctypes
    import msvcrt
    from ctypes import wintypes

    GENERIC_READ = 0x80000000
    FILE_SHARE_READ = 0x00000001
    OPEN_EXISTING = 3
    FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    FILE_ATTRIBUTE_REPARSE_POINT = 0x400
    FILE_ATTRIBUTE_DIRECTORY = 0x00000010
    INVALID_HANDLE = wintypes.HANDLE(-1).value
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    _declare_file_apis(kernel32, advapi32, ctypes, wintypes)
    handle = kernel32.CreateFileW(
        path, GENERIC_READ, FILE_SHARE_READ, None, OPEN_EXISTING,
        FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT, None)
    if not handle or handle == INVALID_HANDLE:
        return ("the file is unreadable: %s"
                % ctypes.WinError(ctypes.get_last_error()), None, None)
    handed_over = False
    try:
        details = _BY_HANDLE_FILE_INFORMATION(ctypes, wintypes)
        if not kernel32.GetFileInformationByHandle(handle,
                                                   ctypes.byref(details)):
            raise ctypes.WinError(ctypes.get_last_error())
        attributes = details.dwFileAttributes
        if attributes & FILE_ATTRIBUTE_REPARSE_POINT:
            return ("a reparse point is not a credential", None, None)
        if attributes & FILE_ATTRIBUTE_DIRECTORY:
            return ("a regular file is required", None, None)
        problem = _windows_security_problem(handle, kernel32, advapi32,
                                            ctypes, wintypes)
        if problem is not None:
            return (problem, None, None)
        descriptor = msvcrt.open_osfhandle(int(handle), os.O_RDONLY)
        handed_over = True
    except OSError as failure:
        return ("the file is unreadable: %s" % failure, None, None)
    finally:
        if not handed_over:
            kernel32.CloseHandle(handle)
    return (None, descriptor, os.fstat(descriptor))


def _BY_HANDLE_FILE_INFORMATION(ctypes, wintypes):
    class Details(ctypes.Structure):
        _fields_ = [("dwFileAttributes", wintypes.DWORD),
                    ("ftCreationTime", wintypes.FILETIME),
                    ("ftLastAccessTime", wintypes.FILETIME),
                    ("ftLastWriteTime", wintypes.FILETIME),
                    ("dwVolumeSerialNumber", wintypes.DWORD),
                    ("nFileSizeHigh", wintypes.DWORD),
                    ("nFileSizeLow", wintypes.DWORD),
                    ("nNumberOfLinks", wintypes.DWORD),
                    ("nFileIndexHigh", wintypes.DWORD),
                    ("nFileIndexLow", wintypes.DWORD)]
    return Details()


def _declare_acl_apis(advapi32, ctypes, wintypes):
    advapi32.GetAclInformation.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.c_int]
    advapi32.GetAclInformation.restype = wintypes.BOOL
    advapi32.GetAce.argtypes = [ctypes.c_void_p, wintypes.DWORD,
                                ctypes.POINTER(ctypes.c_void_p)]
    advapi32.GetAce.restype = wintypes.BOOL


def _declare_file_apis(kernel32, advapi32, ctypes, wintypes):
    """Name every prototype this module calls, handles included."""
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.GetFileInformationByHandle.argtypes = [wintypes.HANDLE,
                                                    ctypes.c_void_p]
    kernel32.GetFileInformationByHandle.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    advapi32.GetSecurityInfo.argtypes = [
        wintypes.HANDLE, ctypes.c_int, wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p)]
    advapi32.GetSecurityInfo.restype = wintypes.DWORD
    _declare_acl_apis(advapi32, ctypes, wintypes)


def _windows_security_problem(handle, kernel32, advapi32, ctypes, wintypes):
    """The owner and every granting entry of the open file's DACL."""
    SE_FILE_OBJECT = 1
    OWNER = 0x00000001
    DACL = 0x00000004
    owner = ctypes.c_void_p()
    dacl = ctypes.c_void_p()
    descriptor = ctypes.c_void_p()
    result = advapi32.GetSecurityInfo(
        handle, SE_FILE_OBJECT, OWNER | DACL, ctypes.byref(owner), None,
        ctypes.byref(dacl), None, ctypes.byref(descriptor))
    # GetSecurityInfo answers with the error code itself, not a BOOL.
    if result:
        raise ctypes.WinError(result)
    try:
        mine = _windows_current_sid()
        if not owner.value or _sid_text(owner) != mine:
            return "a file of mine is required"
        for sid in _windows_acl_grants(dacl, advapi32, ctypes, wintypes):
            if sid != mine and sid not in _PRIVATE_SIDS:
                return "the file is readable by others"
    finally:
        kernel32.LocalFree(descriptor)
    return None


def _windows_current_sid():
    """The SID text of the user this process runs as."""
    import ctypes
    from ctypes import wintypes

    TOKEN_QUERY = 0x0008
    TOKEN_USER = 1
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                          ctypes.POINTER(wintypes.HANDLE)]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(),
                                     TOKEN_QUERY, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        advapi32.GetTokenInformation.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD)]
        advapi32.GetTokenInformation.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        needed = wintypes.DWORD(0)
        advapi32.GetTokenInformation(token, TOKEN_USER, None, 0,
                                     ctypes.byref(needed))
        buffer = ctypes.create_string_buffer(max(1, needed.value))
        if not advapi32.GetTokenInformation(token, TOKEN_USER, buffer,
                                            needed.value,
                                            ctypes.byref(needed)):
            raise ctypes.WinError(ctypes.get_last_error())
        # TOKEN_USER begins with SID_AND_ATTRIBUTES, whose first field is the
        # SID pointer: the same offset on both word sizes.
        sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
        return _sid_text(sid)
    finally:
        kernel32.CloseHandle(token)


def _sid_text(pointer):
    """A SID's string form, allocated and released by the two APIs."""
    import ctypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32.ConvertSidToStringSidW.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p)]
    advapi32.ConvertSidToStringSidW.restype = ctypes.c_int
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    text = ctypes.c_wchar_p()
    if not advapi32.ConvertSidToStringSidW(pointer, ctypes.byref(text)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return text.value
    finally:
        kernel32.LocalFree(text)


def _windows_acl_grants(acl, advapi32, ctypes, wintypes):
    """The SIDs the open file's access-allowed entries grant access to.

    A missing DACL means everyone. An entry that grants access through an
    object type, a condition, or a type this read cannot decode answers as
    the wildcard that no check accepts, because treating an unknown grant
    as absent would approve a file it did not clear.
    """
    ACL_SIZE_INFORMATION = 2
    ACCESS_ALLOWED_ACE_TYPE = 0
    ACCESS_ALLOWED_COMPOUND_ACE_TYPE = 4
    ACCESS_ALLOWED_OBJECT_ACE_TYPE = 5
    ACCESS_ALLOWED_CALLBACK_ACE_TYPE = 9
    ACCESS_ALLOWED_CALLBACK_OBJECT_ACE_TYPE = 11
    # Deny, audit, alarm, label, resource and scoped-policy entries grant
    # nothing this read has to weigh.
    NON_GRANTING = {1, 2, 3, 6, 7, 8, 10, 12, 13, 14, 15, 16, 17, 18, 19}
    UNREADABLE_GRANTS = {ACCESS_ALLOWED_COMPOUND_ACE_TYPE,
                         ACCESS_ALLOWED_OBJECT_ACE_TYPE,
                         ACCESS_ALLOWED_CALLBACK_ACE_TYPE,
                         ACCESS_ALLOWED_CALLBACK_OBJECT_ACE_TYPE}

    class AclSize(ctypes.Structure):
        _fields_ = [("AceCount", wintypes.DWORD),
                    ("AclBytesInUse", wintypes.DWORD),
                    ("AclBytesFree", wintypes.DWORD)]

    class AceHeader(ctypes.Structure):
        _fields_ = [("AceType", ctypes.c_ubyte),
                    ("AceFlags", ctypes.c_ubyte),
                    ("AceSize", wintypes.WORD)]

    if not acl or not acl.value:
        return ("*",)
    size = AclSize()
    if not advapi32.GetAclInformation(acl, ctypes.byref(size),
                                      ctypes.sizeof(size),
                                      ACL_SIZE_INFORMATION):
        raise ctypes.WinError(ctypes.get_last_error())
    grants = []
    for index in range(size.AceCount):
        entry = ctypes.c_void_p()
        if not advapi32.GetAce(acl, index, ctypes.byref(entry)):
            raise ctypes.WinError(ctypes.get_last_error())
        header = ctypes.cast(entry, ctypes.POINTER(AceHeader)).contents
        if header.AceType in UNREADABLE_GRANTS:
            grants.append("*")
            continue
        if header.AceType != ACCESS_ALLOWED_ACE_TYPE:
            if header.AceType not in NON_GRANTING:
                grants.append("*")
            continue
        # ACCESS_ALLOWED_ACE: the mask word, then the SID.
        grants.append(_sid_text(ctypes.c_void_p(entry.value + 8)))
    return grants


def platform_name():
    """This host's name in the seam's one vocabulary: windows or posix."""
    return "windows" if sys.platform.startswith("win") else "posix"


def boot_identity():
    """The kernel's boot identity, or None where the host has none.

    POSIX names the boot so a record from another boot is a different
    host; Windows has no such name, and there the process creation time
    already carries the identity that matters.
    """
    if platform_name() == "windows":
        return None
    try:
        with open("/proc/sys/kernel/random/boot_id", "r",
                  encoding="utf-8") as handle:
            return handle.read().strip() or None
    except OSError:
        return None


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
        # The temp carries both the pid (two concurrent processes
        # converging on one state dir) and the thread id (two threads in
        # one process writing the same file), so no in-flight temp can
        # be truncated or renamed out from under another writer.
        tmp = "%s.tmp.%d.%d" % (self.path, os.getpid(),
                                threading.get_ident())
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
        # proto/caps let a client detect the transcript commands (and an
        # older daemon that omits them) without a second round trip; a
        # peer that ignores them keeps the full-parse fallback.
        return {"ok": True, "srv": "pi-daemon", "proto": 1,
                "caps": ["transcript.v1"], "index": 1}

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
        zombie process counts as gone. Windows uses a handle probe, not a
        signal. Best effort: without os.kill every pid is presumed
        alive."""
        if self.platform.startswith("win"):
            return self._win_pid_alive(pid)
        if not hasattr(os, "kill"):
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

    def _win_process(self, pid):
        """kernel32 with declared prototypes, and an open query handle.

        ctypes truncates a handle to a C int unless restype says
        otherwise, and a truncated handle names no process; every call
        this class makes declares both sides. The handle is None with the
        pending Win32 error beside it when the pid names no process this
        account may query.
        """
        import ctypes
        import ctypes.wintypes as wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL,
                                         wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        if int(pid or 0) <= 0:
            return kernel32, None, None
        handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION,
                                      False, int(pid))
        return kernel32, handle, None if handle else ctypes.get_last_error()

    def _win_pid_alive(self, pid):
        """Windows liveness through a process handle.

        os.kill(pid, 0) is unusable here: on Windows it calls
        TerminateProcess rather than probing (Python issue 14480). A
        successful handle is decided by GetExitCodeProcess, where
        STILL_ACTIVE means alive and any other code means reaped. A
        denied handle names a process that exists and is not ours to
        query, so only a pid that names nothing proves it is gone.
        """
        import ctypes
        import ctypes.wintypes as wintypes
        kernel32, handle, error = self._win_process(pid)
        if not handle:
            return error == _ERROR_ACCESS_DENIED
        kernel32.GetExitCodeProcess.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        try:
            code = wintypes.DWORD(0)
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == _STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)

    def process_start_token(self, pid):
        """A token identifying this exact process incarnation.

        POSIX reads the starttime field (ticks since boot) of
        /proc/<pid>/stat. Windows reads the creation time GetProcessTimes
        reports and serializes it as decimal 100 ns ticks, so no
        precision is lost between the recorded and the compared value. A
        bare signal probe cannot tell a recycled pid from the original
        one; comparing recorded tokens can. Returns None where the start
        time is unavailable.
        """
        if self.platform.startswith("win"):
            return self._win_start_token(pid)
        try:
            with open("/proc/%d/stat" % pid) as fh:
                # Drop "pid (comm) "; the remaining fields start at 3,
                # so overall field 22 (starttime) is index 19.
                return fh.read().rsplit(")", 1)[1].split()[19]
        except (OSError, IndexError):
            return None

    def _win_start_token(self, pid):
        """pid's creation time as decimal ticks, else None."""
        import ctypes
        import ctypes.wintypes as wintypes
        kernel32, handle, _error = self._win_process(pid)
        if not handle:
            return None
        kernel32.GetProcessTimes.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME)]
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        created, exited, kernel, user = (wintypes.FILETIME()
                                         for _name in range(4))
        try:
            if not kernel32.GetProcessTimes(
                    handle, ctypes.byref(created), ctypes.byref(exited),
                    ctypes.byref(kernel), ctypes.byref(user)):
                return None
            return str((created.dwHighDateTime << 32)
                       | created.dwLowDateTime)
        finally:
            kernel32.CloseHandle(handle)

    def pid_matches_token(self, pid, token):
        """True while pid is still the process incarnation the token was
        recorded for. A token that was never recorded (a pre-token
        roster entry, or no /proc on Windows) cannot be disproved, so
        it always matches."""
        current = self.process_start_token(pid)
        if current is None or token is None:
            return True
        return str(current) == str(token)

    def terminate_tree(self, pid, grace, process=None):
        """End a process and its descendants, then wait at most grace.

        `process` is the subprocess child this pid belongs to, when there
        is one: its own poll/wait is the only reaper that may observe it,
        because a waitpid here would steal the exit status the owner
        still needs. A tree that survives the bound, or a termination
        command that could not run while its root lived, raises OSError: a
        caller records a failed ending rather than claiming one that did
        not happen.

        On Windows two budgets apply: the tree kill runs to its own
        command bound (taskkill's five seconds) and only then does grace
        bound the wait for the root to disappear. The nominal budget is
        that bound plus grace; process creation and the timeout cleanup
        can add overhead on top. A failed kill whose root did end is
        reported as ended: nothing here proves a reparented descendant
        died, so the strong guarantee needs a job object.
        """
        if self.platform.startswith("win"):
            self._windows_terminate(pid, grace, process)
        else:
            self._posix_terminate(pid, grace, process)

    def signal_tree(self, pid, number):
        """One signal to one process tree: the single group owner.

        Windows has no signal a group understands, and Popen.kill() and
        terminate() end one process only, so the tree kill is the whole
        meaning the platform carries.
        """
        if self.platform.startswith("win"):
            self.taskkill_tree(pid)
            return
        try:
            group = os.getpgid(pid)
            # A child that has not reached its own session yet still shares
            # ours: signalling that group would take the daemon down with
            # the child it meant to end, so only a foreign group is signalled.
            if group == os.getpgrp():
                os.kill(pid, number)
            else:
                os.killpg(group, number)
        except OSError:
            try:
                os.kill(pid, number)
            except OSError:
                pass

    def signal_winch(self, pid):
        if self.platform.startswith("win"):
            return
        try:
            os.killpg(pid, signal.SIGWINCH)
        except OSError:
            pass

    def signal_term(self, pid):
        self.signal_tree(pid, signal.SIGTERM)

    def signal_kill(self, pid):
        # Windows has no SIGKILL attribute at all; its tree kill ignores
        # which of the two the caller asked for.
        self.signal_tree(pid, getattr(signal, "SIGKILL", signal.SIGTERM))

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

    def _posix_terminate(self, pid, grace, process=None):
        self.signal_term(pid)
        if self._wait_gone(pid, grace, process):
            return
        self.signal_kill(pid)
        if not self._wait_gone(pid, grace, process):
            raise OSError("process %d survived termination" % pid)

    def _wait_gone(self, pid, grace, process=None):
        """Wait out the grace bound; True as soon as the pid is gone."""
        deadline = time.time() + max(0.0, grace)
        while time.time() < deadline:
            if not self._alive(pid, process):
                return True
            time.sleep(0.1)
        return not self._alive(pid, process)

    def _alive(self, pid, process=None):
        """Liveness of a pid, or of the child that pid belongs to."""
        if process is not None:
            return process.poll() is None
        return self.pid_alive(pid)

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
        from ctypes import wintypes
        SYNCHRONIZE = 0x00100000
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        # ctypes defaults a foreign return value to C int, which truncates a
        # 64-bit handle on the way in and out: every call here names its
        # types so the handle survives the round trip.
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL,
                                         wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE,
                                                 wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(SYNCHRONIZE, False, int(pid))
        if not handle:
            return
        try:
            waited = kernel32.WaitForSingleObject(handle, 0xFFFFFFFF)
            if waited not in (0x00000000, 0x00000080):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            kernel32.CloseHandle(handle)

    def _windows_terminate(self, pid, grace, process=None):
        """taskkill the tree on its own bound, then reap the root.

        /T walks the ancestry it finds, so it must run before the root is
        signalled: killing the root first severs the only evidence of its
        descendants. Windows cannot prove ownership of a descendant that
        was reparented after an intermediate ancestor exited; that
        stronger guarantee would need a job object, and /T does not carry
        it. The tree kill keeps its own command bound - a taskkill cut
        short names half a tree - and grace bounds what follows: the wait
        for the root to disappear.
        """
        grace = max(0.0, grace)
        if process is not None and process.poll() is not None:
            # Already reaped by its owner: there is no root left to walk
            # the ancestry from, and no exit status left to steal.
            return
        killed = self.taskkill_tree(pid)
        if process is not None:
            try:
                process.wait(grace)
            except subprocess.TimeoutExpired:
                raise OSError("process %d survived termination" % pid) \
                    from None
        if not killed and self.pid_alive(pid):
            # The root outlived /T, or /T could not run: the descendants it
            # would have named are still unaccounted for. A failed /T whose
            # root did end is reported as ended, which is all this can show:
            # nothing here proves a reparented descendant died.
            raise OSError("could not terminate process %d" % pid)

    def taskkill_tree(self, pid, timeout=5):
        """Windows tree kill: one taskkill /T /F owner for the daemon's
        signal paths and the ConPTY backend's child teardown. True when
        taskkill ran and reported that it ended the tree."""
        try:
            result = subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                timeout=timeout)
        except (OSError, subprocess.SubprocessError):
            return False
        return result.returncode == 0


class PtyChild:
    """One child on a pseudo-terminal: the only owner of its own I/O.

    The relay reads, writes and resizes through these operations; the
    runner keeps the process itself, so `release` is what lets a recorded
    child start and `wait` is the one reaper of its exit status.
    """

    pid = 0

    def output_handle(self):
        """A descriptor the relay's read loop can select on."""
        raise NotImplementedError

    def read_output(self, limit):
        raise NotImplementedError

    def write_input(self, data):
        raise NotImplementedError

    def set_winsize(self, cols, rows):
        raise NotImplementedError

    def release(self):
        """Let a child spawned gated or suspended start running."""
        raise NotImplementedError

    def poll(self):
        """Its exit code once it is reaped, else None."""
        raise NotImplementedError

    def wait(self, timeout=None):
        """Reap it and return its subprocess-style exit code."""
        raise NotImplementedError

    def terminate(self, grace=None):
        """End its process tree; False when it survived the bound."""
        raise NotImplementedError

    def close(self):
        """Close this child's own I/O; it never signals the process."""

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


def select_pty_backend(config=None, platform=None):
    """The one PTY backend for this platform: the gated POSIX helper, or
    the lazy ConPTY one. Both are selected here, so no domain names a
    platform-specific implementation of its own."""
    platform = sys.platform if platform is None else platform
    if platform.startswith("win"):
        from pi_conpty import WindowsPtyBackend
        return WindowsPtyBackend()
    from pi_daemon.sessions.pty_backend import PosixPtyBackend
    return PosixPtyBackend(config)


def select_terminal_mode(platform=None):
    """The attach terminal seam for this platform."""
    platform = sys.platform if platform is None else platform
    if platform.startswith("win"):
        from pi_conpty import WindowsConsole
        return WindowsConsole()
    return PosixTerminal()
#!/usr/bin/env python3
# pi/lib/pi_conpty.py - Windows ConPTY backend for the pi-daemon PTY host.
#
# Pure-stdlib ctypes implementation of the Windows pseudoconsole seam.
# It exposes the same duck-typed PtyBackend/PtyChild contract as the
# POSIX backend in pi_platform.py, so pi_platform can lazily import it on
# Windows without the POSIX path noticing anything. kernel32 is loaded
# only behind a sys.platform check, and every import above is
# cross-platform, so importing this module on Linux is safe.
#
# WindowsPtyChild gives the host three things it needs:
#   * output_fd  - one end of a socketpair, select-able on Windows, fed by
#                  a reader thread that pumps the ConPTY output pipe;
#   * master_fd  - a CRT fd over the ConPTY input HANDLE for os.write;
#   * pid + wait_nohang/kill_tree - non-blocking process reaping.
#
# Spawn follows node-pty src/win/conpty.cc: CreatePseudoConsole, then a
# STARTUPINFOEXW carrying PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE and
# STARTF_USESTDHANDLES with NULL std handles, then CreateProcessW with
# EXTENDED_STARTUPINFO_PRESENT | CREATE_UNICODE_ENVIRONMENT.

import ctypes
import ctypes.wintypes as wintypes
import os
import socket
import subprocess
import sys
import threading
import time

_IS_WINDOWS = sys.platform == "win32"

# -- Win32 constants -------------------------------------------------------

_EXTENDED_STARTUPINFO_PRESENT = 0x00080000
_CREATE_UNICODE_ENVIRONMENT = 0x00000400
_STARTF_USESTDHANDLES = 0x00000100
_PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE = 0x00020016

_WAIT_OBJECT_0 = 0x00000000

_EVENT_KEY = 0x0001
_EVENT_WINDOW_BUFFER_SIZE = 0x0004

_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_OPEN_EXISTING = 3
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

_ENABLE_PROCESSED_INPUT = 0x0001
_ENABLE_LINE_INPUT = 0x0002
_ENABLE_ECHO_INPUT = 0x0004
_ENABLE_WINDOW_INPUT = 0x0008
_ENABLE_VIRTUAL_TERMINAL_INPUT = 0x0200
_ENABLE_PROCESSED_OUTPUT = 0x0001
_ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004

DEFAULT_COLS = 80
DEFAULT_ROWS = 24

_READ_CHUNK = 65536
_CONSOLE_EVENTS = 64
_KILL_TICK = 0.05


# -- ctypes structures (module-private helpers) ----------------------------

class _COORD(ctypes.Structure):
    """Win32 COORD: SHORT x, SHORT y."""

    _fields_ = [
        ("X", wintypes.SHORT),
        ("Y", wintypes.SHORT),
    ]


class _SMALL_RECT(ctypes.Structure):
    """Win32 SMALL_RECT."""

    _fields_ = [
        ("Left", wintypes.SHORT),
        ("Top", wintypes.SHORT),
        ("Right", wintypes.SHORT),
        ("Bottom", wintypes.SHORT),
    ]


class _STARTUPINFOW(ctypes.Structure):
    """Win32 STARTUPINFOW."""

    _fields_ = [
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD),
        ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.POINTER(ctypes.c_byte)),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class _STARTUPINFOEXW(ctypes.Structure):
    """Win32 STARTUPINFOEXW: STARTUPINFOW plus the attribute list."""

    _fields_ = [
        ("StartupInfo", _STARTUPINFOW),
        ("lpAttributeList", ctypes.c_void_p),
    ]


class _PROCESS_INFORMATION(ctypes.Structure):
    """Win32 PROCESS_INFORMATION."""

    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]


class _KEY_EVENT_RECORD(ctypes.Structure):
    """Win32 KEY_EVENT_RECORD."""

    _fields_ = [
        ("bKeyDown", wintypes.BOOL),
        ("wRepeatCount", wintypes.WORD),
        ("wVirtualKeyCode", wintypes.WORD),
        ("wVirtualScanCode", wintypes.WORD),
        ("UnicodeChar", wintypes.WCHAR),
        ("dwControlKeyState", wintypes.DWORD),
    ]


class _MOUSE_EVENT_RECORD(ctypes.Structure):
    """Win32 MOUSE_EVENT_RECORD (sizes the input-record union)."""

    _fields_ = [
        ("dwMousePosition", _COORD),
        ("dwButtonState", wintypes.DWORD),
        ("dwControlKeyState", wintypes.DWORD),
        ("dwEventFlags", wintypes.DWORD),
    ]


class _WINDOW_BUFFER_SIZE_RECORD(ctypes.Structure):
    """Win32 WINDOW_BUFFER_SIZE_RECORD."""

    _fields_ = [
        ("dwSize", _COORD),
    ]


class _INPUT_EVENT(ctypes.Union):
    """Win32 INPUT_RECORD Event union."""

    _fields_ = [
        ("KeyEvent", _KEY_EVENT_RECORD),
        ("MouseEvent", _MOUSE_EVENT_RECORD),
        ("WindowBufferSizeEvent", _WINDOW_BUFFER_SIZE_RECORD),
    ]


class _INPUT_RECORD(ctypes.Structure):
    """Win32 INPUT_RECORD."""

    _fields_ = [
        ("EventType", wintypes.WORD),
        ("Event", _INPUT_EVENT),
    ]


class _CONSOLE_SCREEN_BUFFER_INFO(ctypes.Structure):
    """Win32 CONSOLE_SCREEN_BUFFER_INFO."""

    _fields_ = [
        ("dwSize", _COORD),
        ("dwCursorPosition", _COORD),
        ("wAttributes", wintypes.WORD),
        ("srWindow", _SMALL_RECT),
        ("dwMaximumWindowSize", _COORD),
    ]


def _environment_block(env):
    """CREATE_UNICODE_ENVIRONMENT block: UTF-16, double-NUL terminated."""
    items = ["%s=%s" % (key, value) for key, value in (env or {}).items()]
    items.sort(key=lambda item: item.upper())
    return ctypes.create_unicode_buffer("\0".join(items) + "\0\0")


class _Kernel32:
    """Loads kernel32 and binds the ConPTY + console prototypes."""

    def __init__(self):
        if not _IS_WINDOWS:
            raise OSError("ConPTY is only available on Windows")
        try:
            self.lib = ctypes.WinDLL("kernel32", use_last_error=True)
            self._bind_conpty()
            self._bind_console()
        except AttributeError as exc:
            raise OSError("ConPTY API unavailable: %s" % (exc,))

    def _bind_conpty(self):
        lib = self.lib
        pointer = ctypes.POINTER
        void_p = ctypes.c_void_p
        lib.CreatePseudoConsole.argtypes = [
            _COORD, wintypes.HANDLE, wintypes.HANDLE, wintypes.DWORD,
            pointer(wintypes.HANDLE)]
        lib.CreatePseudoConsole.restype = ctypes.c_long
        lib.ResizePseudoConsole.argtypes = [wintypes.HANDLE, _COORD]
        lib.ResizePseudoConsole.restype = ctypes.c_long
        lib.ClosePseudoConsole.argtypes = [wintypes.HANDLE]
        lib.ClosePseudoConsole.restype = None
        lib.CreatePipe.argtypes = [
            pointer(wintypes.HANDLE), pointer(wintypes.HANDLE), void_p,
            wintypes.DWORD]
        lib.CreatePipe.restype = wintypes.BOOL
        lib.CreateProcessW.argtypes = [
            wintypes.LPCWSTR, wintypes.LPWSTR, void_p, void_p, wintypes.BOOL,
            wintypes.DWORD, void_p, wintypes.LPCWSTR, void_p,
            pointer(_PROCESS_INFORMATION)]
        lib.CreateProcessW.restype = wintypes.BOOL
        lib.InitializeProcThreadAttributeList.argtypes = [
            void_p, wintypes.DWORD, wintypes.DWORD,
            pointer(ctypes.c_size_t)]
        lib.InitializeProcThreadAttributeList.restype = wintypes.BOOL
        lib.UpdateProcThreadAttribute.argtypes = [
            void_p, wintypes.DWORD, ctypes.c_size_t, void_p,
            ctypes.c_size_t, void_p, void_p]
        lib.UpdateProcThreadAttribute.restype = wintypes.BOOL
        lib.DeleteProcThreadAttributeList.argtypes = [void_p]
        lib.DeleteProcThreadAttributeList.restype = None
        lib.ReadFile.argtypes = [
            wintypes.HANDLE, void_p, wintypes.DWORD,
            pointer(wintypes.DWORD), void_p]
        lib.ReadFile.restype = wintypes.BOOL
        lib.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        lib.WaitForSingleObject.restype = wintypes.DWORD
        lib.GetExitCodeProcess.argtypes = [
            wintypes.HANDLE, pointer(wintypes.DWORD)]
        lib.GetExitCodeProcess.restype = wintypes.BOOL
        lib.TerminateProcess.argtypes = [wintypes.HANDLE, ctypes.c_uint]
        lib.TerminateProcess.restype = wintypes.BOOL
        lib.CloseHandle.argtypes = [wintypes.HANDLE]
        lib.CloseHandle.restype = wintypes.BOOL

    def _bind_console(self):
        lib = self.lib
        pointer = ctypes.POINTER
        void_p = ctypes.c_void_p
        lib.CreateFileW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, void_p,
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        lib.CreateFileW.restype = wintypes.HANDLE
        lib.GetConsoleMode.argtypes = [
            wintypes.HANDLE, pointer(wintypes.DWORD)]
        lib.GetConsoleMode.restype = wintypes.BOOL
        lib.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        lib.SetConsoleMode.restype = wintypes.BOOL
        lib.GetConsoleScreenBufferInfo.argtypes = [
            wintypes.HANDLE, pointer(_CONSOLE_SCREEN_BUFFER_INFO)]
        lib.GetConsoleScreenBufferInfo.restype = wintypes.BOOL
        lib.PeekConsoleInputW.argtypes = [
            wintypes.HANDLE, pointer(_INPUT_RECORD), wintypes.DWORD,
            pointer(wintypes.DWORD)]
        lib.PeekConsoleInputW.restype = wintypes.BOOL
        lib.ReadConsoleInputW.argtypes = [
            wintypes.HANDLE, pointer(_INPUT_RECORD), wintypes.DWORD,
            pointer(wintypes.DWORD)]
        lib.ReadConsoleInputW.restype = wintypes.BOOL


def _raise_last_error(message):
    """Raise an OSError carrying the pending Win32 error code."""
    code = ctypes.get_last_error()
    raise OSError(code, "%s: %s" % (message, ctypes.FormatError(code)))


class WindowsPtyChild:
    """One child on one pseudoconsole; all state is instance-owned."""

    def __init__(self):
        self.pid = 0
        self.output_fd = -1
        self.master_fd = -1
        self._api = None
        self._hpc = None
        self._input_read = None
        self._input_write = None
        self._output_read = None
        self._output_write = None
        self._process = None
        self._out_sock = None
        self._reader_sock = None
        self._reader_thread = None
        self._reaped = False
        self._exit_code = 0
        self._closed = False

    # -- spawn -------------------------------------------------------------

    def start(self, api, argv, cwd, env, cols, rows):
        """Spawn argv on a fresh pseudoconsole of the given size."""
        self._api = api
        lib = api.lib
        try:
            self._make_pipes(lib)
            self._make_pseudoconsole(lib, cols, rows)
            lib.CloseHandle(self._input_read)
            self._input_read = None
            lib.CloseHandle(self._output_write)
            self._output_write = None
            si_ex, attr_buf = self._make_startupinfo(lib)
            self._create_process(lib, argv, cwd, env, si_ex, attr_buf)
            self._open_master_fd()
            self._start_reader()
        except Exception:
            self.close()
            raise

    def _make_pipes(self, lib):
        h_in_read = wintypes.HANDLE()
        h_in_write = wintypes.HANDLE()
        h_out_read = wintypes.HANDLE()
        h_out_write = wintypes.HANDLE()
        if not lib.CreatePipe(ctypes.byref(h_in_read),
                              ctypes.byref(h_in_write), None, 0):
            _raise_last_error("CreatePipe(input)")
        # Store each pipe as it is created so a later failure's close()
        # releases the handles already opened.
        self._input_read = h_in_read.value
        self._input_write = h_in_write.value
        if not lib.CreatePipe(ctypes.byref(h_out_read),
                              ctypes.byref(h_out_write), None, 0):
            _raise_last_error("CreatePipe(output)")
        self._output_read = h_out_read.value
        self._output_write = h_out_write.value

    def _make_pseudoconsole(self, lib, cols, rows):
        hpc = wintypes.HANDLE()
        result = lib.CreatePseudoConsole(
            _COORD(cols, rows), self._input_read, self._output_write, 0,
            ctypes.byref(hpc))
        if result < 0:
            raise OSError("CreatePseudoConsole failed: 0x%08X"
                          % (result & 0xFFFFFFFF))
        self._hpc = hpc

    def _make_startupinfo(self, lib):
        size = ctypes.c_size_t(0)
        lib.InitializeProcThreadAttributeList(None, 1, 0,
                                              ctypes.byref(size))
        buffer = ctypes.create_string_buffer(size.value)
        pointer = ctypes.cast(buffer, ctypes.c_void_p)
        if not lib.InitializeProcThreadAttributeList(
                pointer, 1, 0, ctypes.byref(size)):
            _raise_last_error("InitializeProcThreadAttributeList")
        # lpValue must be the HPCON value itself, not a pointer to it;
        # a byref here silently detaches the child from the pseudoconsole.
        update_ok = lib.UpdateProcThreadAttribute(
            pointer, 0, _PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE,
            self._hpc, ctypes.sizeof(wintypes.HANDLE),
            None, None)
        if not update_ok:
            lib.DeleteProcThreadAttributeList(pointer)
            _raise_last_error("UpdateProcThreadAttribute")
        si_ex = _STARTUPINFOEXW()
        si_ex.StartupInfo.cb = ctypes.sizeof(_STARTUPINFOEXW)
        si_ex.StartupInfo.dwFlags = _STARTF_USESTDHANDLES
        si_ex.StartupInfo.hStdInput = None
        si_ex.StartupInfo.hStdOutput = None
        si_ex.StartupInfo.hStdError = None
        si_ex.lpAttributeList = pointer
        # The buffer must outlive CreateProcessW; hand it back so the
        # caller keeps a reference while the child is created.
        return si_ex, buffer

    def _create_process(self, lib, argv, cwd, env, si_ex, attr_buf):
        command = ctypes.create_unicode_buffer(
            subprocess.list2cmdline(list(argv)))
        env_block = _environment_block(env)
        cwd_block = ctypes.c_wchar_p(cwd) if cwd else None
        info = _PROCESS_INFORMATION()
        created = lib.CreateProcessW(
            None, command, None, None, False,
            _EXTENDED_STARTUPINFO_PRESENT | _CREATE_UNICODE_ENVIRONMENT,
            env_block, cwd_block, ctypes.byref(si_ex), ctypes.byref(info))
        lib.DeleteProcThreadAttributeList(si_ex.lpAttributeList)
        del attr_buf
        if not created:
            _raise_last_error("CreateProcessW")
        self._process = info.hProcess
        self.pid = int(info.dwProcessId)
        lib.CloseHandle(info.hThread)

    def _open_master_fd(self):
        import msvcrt
        try:
            fd = msvcrt.open_osfhandle(self._input_write, os.O_WRONLY)
        except Exception:
            raise
        else:
            self.master_fd = fd
            self._input_write = None

    def _start_reader(self):
        self._out_sock, self._reader_sock = socket.socketpair()
        self.output_fd = self._out_sock.fileno()
        self._reader_thread = threading.Thread(
            target=self._reader_loop, daemon=True)
        self._reader_thread.start()

    def _reader_loop(self):
        while True:
            data = self._read_pipe()
            if not data:
                break
            try:
                self._reader_sock.sendall(data)
            except OSError:
                break
        try:
            self._reader_sock.shutdown(socket.SHUT_WR)
        except OSError:
            pass

    def _read_pipe(self):
        buffer = ctypes.create_string_buffer(_READ_CHUNK)
        read = wintypes.DWORD(0)
        ok = self._api.lib.ReadFile(self._output_read, buffer, _READ_CHUNK,
                                    ctypes.byref(read), None)
        if not ok or read.value == 0:
            return b""
        return buffer.raw[:read.value]

    # -- PtyChild interface ------------------------------------------------

    def output_handle(self):
        return self.output_fd

    def read_output(self, limit):
        if self._out_sock is None:
            return b""
        try:
            return self._out_sock.recv(limit)
        except OSError:
            return b""

    def write_input(self, data):
        if self.master_fd < 0:
            return 0
        try:
            return os.write(self.master_fd, data)
        except OSError:
            return 0

    def resize(self, cols, rows):
        if self._hpc is None:
            return False
        result = self._api.lib.ResizePseudoConsole(
            self._hpc, _COORD(cols, rows))
        if result < 0:
            raise OSError("ResizePseudoConsole failed: 0x%08X"
                          % (result & 0xFFFFFFFF))
        return True

    def set_winsize(self, cols, rows):
        try:
            self.resize(cols, rows)
        except OSError:
            pass

    def get_winsize(self):
        return (0, 0)

    def signal_winch(self):
        pass

    def wait_nohang(self):
        if self._reaped:
            return (True, self._exit_code)
        if self._process is None:
            return (False, 0)
        wait = self._api.lib.WaitForSingleObject(self._process, 0)
        if wait != _WAIT_OBJECT_0:
            return (False, 0)
        code = wintypes.DWORD(0)
        if not self._api.lib.GetExitCodeProcess(
                self._process, ctypes.byref(code)):
            return (False, 0)
        self._reaped = True
        self._exit_code = int(code.value)
        return (True, self._exit_code)

    def kill_tree(self, grace):
        if self._process is None:
            return
        if self.wait_nohang()[0]:
            return
        self._terminate_process()
        self._taskkill_tree(grace)
        self._wait_reaped(grace)

    def terminate(self, grace=0.5):
        self.kill_tree(grace)

    def _terminate_process(self):
        self._api.lib.TerminateProcess(self._process, 1)

    def _taskkill_tree(self, grace):
        if not _IS_WINDOWS:
            return
        try:
            subprocess.run(
                ["taskkill", "/PID", str(self.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                timeout=max(0.0, grace))
        except (OSError, subprocess.SubprocessError):
            pass

    def _wait_reaped(self, grace):
        deadline = time.time() + max(0.0, grace)
        while time.time() < deadline:
            if self.wait_nohang()[0]:
                return
            time.sleep(_KILL_TICK)

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._close_pseudoconsole()
        self._close_reader()
        self._close_output_pipe()
        self._close_master_fd()
        self._close_process_handles()

    def _close_pseudoconsole(self):
        if self._hpc is None:
            return
        try:
            self._api.lib.ClosePseudoConsole(self._hpc)
        except Exception:
            pass
        self._hpc = None

    def _close_reader(self):
        if self._reader_sock is not None:
            try:
                self._reader_sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        if self._reader_thread is not None:
            self._reader_thread.join(timeout=1.0)
            self._reader_thread = None
        for sock in (self._out_sock, self._reader_sock):
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
        self._out_sock = None
        self._reader_sock = None
        self.output_fd = -1

    def _close_output_pipe(self):
        if self._output_read is not None and self._api is not None:
            self._api.lib.CloseHandle(self._output_read)
        self._output_read = None
        if self._output_write is not None and self._api is not None:
            self._api.lib.CloseHandle(self._output_write)
        self._output_write = None

    def _close_master_fd(self):
        if self.master_fd >= 0:
            try:
                os.close(self.master_fd)
            except OSError:
                pass
            self.master_fd = -1
        if self._input_write is not None and self._api is not None:
            self._api.lib.CloseHandle(self._input_write)
        self._input_write = None
        if self._input_read is not None and self._api is not None:
            try:
                self._api.lib.CloseHandle(self._input_read)
            except Exception:
                pass
        self._input_read = None

    def _close_process_handles(self):
        if self._process is not None and self._api is not None:
            self._api.lib.CloseHandle(self._process)
        self._process = None


class WindowsPtyBackend:
    """ConPTY factory: one pseudoconsole per spawned child."""

    name = "windows"

    def __init__(self):
        self._api = _Kernel32()

    @classmethod
    def available(cls):
        if not _IS_WINDOWS:
            return False
        try:
            _Kernel32()
        except Exception:
            return False
        return True

    def spawn(self, argv, cwd, env, cols=DEFAULT_COLS, rows=DEFAULT_ROWS):
        child = WindowsPtyChild()
        child.start(self._api, argv, cwd, env, cols, rows)
        return child


class WindowsConsole:
    """Raw CONIN$/CONOUT$ seam for the Windows pi-rc attach client."""

    def __init__(self):
        self._api = None
        self._in_handle = None
        self._out_handle = None
        self._saved_in_mode = None
        self._saved_out_mode = None
        self._input_buf = bytearray()

    # -- TerminalMode interface -------------------------------------------

    def enter(self):
        self.enter_raw()

    def enter_raw(self):
        self._api = _Kernel32()
        lib = self._api.lib
        self._in_handle = self._open_console(
            "CONIN$", _GENERIC_READ | _GENERIC_WRITE)
        self._out_handle = self._open_console(
            "CONOUT$", _GENERIC_READ | _GENERIC_WRITE)
        self._saved_in_mode = self._get_mode(self._in_handle)
        self._saved_out_mode = self._get_mode(self._out_handle)
        in_mode = (self._saved_in_mode
                   & ~(_ENABLE_LINE_INPUT | _ENABLE_ECHO_INPUT
                       | _ENABLE_PROCESSED_INPUT)
                   | _ENABLE_WINDOW_INPUT
                   | _ENABLE_VIRTUAL_TERMINAL_INPUT)
        out_mode = (self._saved_out_mode | _ENABLE_PROCESSED_OUTPUT
                    | _ENABLE_VIRTUAL_TERMINAL_PROCESSING)
        self._set_mode(self._in_handle, in_mode)
        self._set_mode(self._out_handle, out_mode)

    def restore(self):
        if self._in_handle is not None and self._saved_in_mode is not None:
            self._set_mode(self._in_handle, self._saved_in_mode)
            self._saved_in_mode = None
        if self._out_handle is not None and self._saved_out_mode is not None:
            self._set_mode(self._out_handle, self._saved_out_mode)
            self._saved_out_mode = None
        self._close_console()

    def size(self):
        if self._out_handle is None:
            return (0, 0)
        info = _CONSOLE_SCREEN_BUFFER_INFO()
        if not self._api.lib.GetConsoleScreenBufferInfo(
                self._out_handle, ctypes.byref(info)):
            return (0, 0)
        cols = info.srWindow.Right - info.srWindow.Left + 1
        rows = info.srWindow.Bottom - info.srWindow.Top + 1
        return (cols, rows)

    # -- resize + input ---------------------------------------------------

    def poll_resize(self):
        """Drain pending console input; True if a buffer-size event seen."""
        if self._in_handle is None:
            return False
        peek = (_INPUT_RECORD * _CONSOLE_EVENTS)()
        seen = wintypes.DWORD(0)
        if not self._api.lib.PeekConsoleInputW(
                self._in_handle, peek, _CONSOLE_EVENTS,
                ctypes.byref(seen)):
            return False
        if seen.value == 0:
            return False
        records = (_INPUT_RECORD * seen.value)()
        got = wintypes.DWORD(0)
        if not self._api.lib.ReadConsoleInputW(
                self._in_handle, records, seen.value, ctypes.byref(got)):
            return False
        resized = False
        for index in range(got.value):
            record = records[index]
            if record.EventType == _EVENT_WINDOW_BUFFER_SIZE:
                resized = True
            elif record.EventType == _EVENT_KEY:
                self._buffer_key(record.Event.KeyEvent)
        return resized

    def _buffer_key(self, key):
        char = key.UnicodeChar
        if key.bKeyDown and char:
            self._input_buf.extend(char.encode("utf-8", "replace"))

    def read_input(self, limit=_READ_CHUNK):
        take = bytes(self._input_buf[:limit])
        del self._input_buf[:len(take)]
        return take

    # -- handles ----------------------------------------------------------

    def _open_console(self, name, access):
        handle = self._api.lib.CreateFileW(
            name, access, _FILE_SHARE_READ | _FILE_SHARE_WRITE, None,
            _OPEN_EXISTING, 0, None)
        if handle in (None, _INVALID_HANDLE_VALUE):
            _raise_last_error("CreateFileW(%s)" % name)
        return handle

    def _get_mode(self, handle):
        mode = wintypes.DWORD(0)
        if not self._api.lib.GetConsoleMode(handle, ctypes.byref(mode)):
            _raise_last_error("GetConsoleMode")
        return mode.value

    def _set_mode(self, handle, mode):
        if not self._api.lib.SetConsoleMode(handle, mode):
            _raise_last_error("SetConsoleMode")

    def _close_console(self):
        for name in ("_in_handle", "_out_handle"):
            handle = getattr(self, name)
            if handle is not None and self._api is not None:
                self._api.lib.CloseHandle(handle)
            setattr(self, name, None)

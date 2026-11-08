"""One ssh child per configured peer, and the marker that proves it bound.

The child runs in its own process group with a foreground forward. Readiness
is the marker its local `PermitLocalCommand` echoes only after every local
forward is bound, so a stolen port is never adopted; the authenticated hello
over the resulting socket is what makes the link up, not the tunnel.
"""

import os
import secrets
import signal
import socket
import subprocess
import threading

from .peer_tunnel_error import TunnelError

MARKER = "pi-peer-ready-"
STDERR_BYTES = 8192


class PeerTunnel:
    """It owns one child process; it never speaks the RPC protocol."""

    def __init__(self, config, spawn=None):
        self._config = config
        self._spawn = spawn or subprocess.Popen
        self._lock = threading.Lock()
        self._process = None
        self._ready = threading.Event()
        self._stderr = bytearray()
        self._readers = []
        self._local_port = 0

    def start(self, peer):
        """Launch the forward and wait for its readiness marker."""
        with self._lock:
            if self._process is not None:
                raise TunnelError("already-started")
            port = int(peer.get("localPort") or 0) or self._free_port()
            nonce = secrets.token_hex(8)
            argv = self._argv(peer, port, nonce)
            try:
                process = self._spawn(
                    argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, shell=False,
                    start_new_session=True)
            except (OSError, ValueError) as failure:
                raise TunnelError(f"spawn-failed: {failure}") from None
            self._process = process
            self._local_port = port
            self._ready.clear()
            self._stderr = bytearray()
            self._start_reader(process.stdout, self._scan_stdout, nonce)
            self._start_reader(process.stderr, self._scan_stderr, None)
            if not self._ready.wait(self._config.comms_peer_ready_timeout):
                detail = self.stderr_text()
                self._shutdown()
                reason = "local-port-in-use" if "address already in use" \
                    in detail.lower() else "tunnel-timeout"
                raise TunnelError(f"{reason}: {detail}".strip())
            return ("127.0.0.1", port)

    def address(self):
        return ("127.0.0.1", self._local_port)

    def alive(self):
        process = self._process
        return process is not None and process.poll() is None

    def stderr_text(self):
        return bytes(self._stderr).decode("utf-8", "replace").strip()

    def close(self):
        """Terminate the whole process group and reap it; report survival."""
        with self._lock:
            return self._shutdown()

    def _shutdown(self):
        """The one close path, for callers that already hold the lock.

        A failed start calls it while still holding the lock, so it must never
        take the lock itself: a non-reentrant lock would deadlock the caller.
        """
        process, self._process = self._process, None
        self._ready.set()
        for reader in self._readers:
            reader.join(self._config.stop_grace)
        self._readers = []
        if process is None:
            return True
        clean = self._terminate(process)
        for stream in (process.stdout, process.stderr):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass
        return clean

    def _terminate(self, process):
        if process.poll() is None:
            self._signal_group(process, signal.SIGTERM)
            try:
                process.wait(self._config.stop_grace)
            except subprocess.TimeoutExpired:
                self._signal_group(process, signal.SIGKILL)
                try:
                    process.wait(self._config.stop_grace)
                except subprocess.TimeoutExpired:
                    return False
        try:
            process.wait(0)
        except subprocess.TimeoutExpired:
            return False
        return True

    @staticmethod
    def _signal_group(process, number):
        try:
            os.killpg(os.getpgid(process.pid), number)
        except OSError:
            pass

    def _free_port(self):
        """Reserve an ephemeral port, then release it for the child to bind."""
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.bind(("127.0.0.1", 0))
            return probe.getsockname()[1]
        finally:
            probe.close()

    def _argv(self, peer, port, nonce):
        config = self._config
        return [
            config.comms_peer_ssh_command, "-N", "-T",
            "-o", "BatchMode=yes",
            "-o", "ExitOnForwardFailure=yes",
            "-o", "ClearAllForwardings=no",
            "-o", "ControlMaster=no",
            "-o", "ControlPath=none",
            "-o", "ControlPersist=no",
            "-o", "ForkAfterAuthentication=yes",
            "-o", "StrictHostKeyChecking=yes",
            "-o", "ForwardAgent=no",
            "-o", "ForwardX11=no",
            "-o", "PermitLocalCommand=yes",
            "-o", f"LocalCommand=echo {MARKER}{nonce}",
            "-o", f"ConnectTimeout={int(config.connect_timeout)}",
            "-o", f"ServerAliveInterval={config.comms_peer_keepalive_interval}",
            "-o", f"ServerAliveCountMax={config.comms_peer_keepalive_failures}",
            "-L", f"127.0.0.1:{port}:127.0.0.1:{peer['remotePort']}",
            peer["sshTarget"],
        ]

    def _start_reader(self, stream, scan, marker):
        thread = threading.Thread(target=self._read_stream,
                                  args=(stream, scan, marker),
                                  name="peer-tunnel", daemon=True)
        self._readers.append(thread)
        thread.start()

    def _read_stream(self, stream, scan, marker):
        try:
            for line in iter(stream.readline, b""):
                scan(line, marker)
        except (OSError, ValueError):
            pass

    def _scan_stdout(self, line, marker):
        if marker and (MARKER + marker).encode("ascii") in line:
            self._ready.set()

    def _scan_stderr(self, line, _marker):
        limit = min(self._config.comms_peer_stderr_bytes, STDERR_BYTES)
        self._stderr.extend(line)
        if len(self._stderr) > limit:
            del self._stderr[:-limit]

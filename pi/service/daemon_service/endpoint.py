"""The control endpoint: loopback TCP, one authorized connection, one
newline-delimited JSON request per line.

The endpoint owns sockets, the token check, and the handshake; a router
handler never sees a connection. Coordinates and the per-daemon token
are published in the runtime endpoint file (mode 0600) so a client
discovers them instead of assuming a port.
"""

import json
import os
import socket
import threading

from . import wire
from .state import atomic_write

HOST = "127.0.0.1"
LISTEN_BACKLOG = 16
WIRE_VERSION = 1


class Endpoint:
    """Owns the listener, the published coordinates, and the handshake."""

    def __init__(self, path, router, token, state, read_timeout=30.0):
        self.path = path
        self.router = router
        self.token = token
        self.state = state
        self.read_timeout = read_timeout
        self.port = None
        self.listener = None
        self.stopping = threading.Event()
        self.connections = set()
        self.connections_lock = threading.Lock()
        self.acceptor = None

    def start(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((HOST, 0))
        listener.listen(LISTEN_BACKLOG)
        self.listener = listener
        self.port = listener.getsockname()[1]
        self._publish()
        self.acceptor = threading.Thread(target=self._accept_loop, daemon=True)
        self.acceptor.start()

    def _publish(self):
        atomic_write(self.path, self._coordinates_text(), mode=0o600)

    def _coordinates_text(self):
        return json.dumps({"host": HOST, "port": self.port,
                           "token": self.token, "version": WIRE_VERSION})

    def stop(self):
        self.stopping.set()
        if self.listener is not None:
            try:
                self.listener.close()
            except OSError:
                pass
        with self.connections_lock:
            connections = list(self.connections)
        for conn in connections:
            try:
                conn.close()
            except OSError:
                pass
        self._unpublish()

    def _unpublish(self):
        data = self.state.read_json(self.path)
        if data is not None and data.get("port") != self.port:
            return  # a successor already published over us
        try:
            os.unlink(self.path)
        except OSError:
            pass

    def _accept_loop(self):
        while not self.stopping.is_set():
            try:
                conn, _addr = self.listener.accept()
            except OSError:
                return
            worker = threading.Thread(target=self._serve, args=(conn,),
                                      daemon=True)
            with self.connections_lock:
                self.connections.add(conn)
            worker.start()

    def _serve(self, conn):
        try:
            conn.settimeout(self.read_timeout)
            if not self._handshake(conn):
                return
            self._request_loop(conn)
        except OSError:
            pass
        finally:
            with self.connections_lock:
                self.connections.discard(conn)
            try:
                conn.close()
            except OSError:
                pass

    def _handshake(self, conn):
        message = self._read_request(conn)
        if message is None or message.get("cmd") != "hello":
            self._send(conn, wire.fail("unauthorized"))
            return False
        if message.get("token") != self.token:
            self._send(conn, wire.fail("unauthorized"))
            return False
        if message.get("version") != WIRE_VERSION:
            self._send(conn, wire.fail("protocol-mismatch"))
            return False
        self._send(conn, wire.ok(**self.router.identity()))
        return True

    def _request_loop(self, conn):
        while not self.stopping.is_set():
            message = self._read_request(conn)
            if message is None:
                return
            self._send(conn, self.router.serve(message))

    def _read_request(self, conn):
        chunks = []
        size = 0
        while True:
            piece = conn.recv(4096)
            if not piece:
                return None
            size += len(piece)
            if size > wire.MAX_LINE:
                return None
            chunks.append(piece)
            data = b"".join(chunks)
            if b"\n" in data:
                line = data.split(b"\n", 1)[0]
                return wire.decode(line.decode("utf-8", "replace"))
            if size > wire.MAX_LINE:
                return None

    def _send(self, conn, message):
        try:
            conn.sendall(wire.encode(message))
        except OSError:
            pass


def probe(path, timeout=0.5):
    """True when a live service answers at the published coordinates."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return False
        sock = socket.create_connection((data["host"], int(data["port"])),
                                        timeout=timeout)
    except (OSError, ValueError, KeyError):
        return False
    try:
        sock.sendall(wire.encode({"cmd": "hello",
                                  "token": data.get("token"),
                                  "version": data.get("version")}))
        reply = wire.decode(sock.recv(65536).decode("utf-8", "replace")
                            .split("\n", 1)[0])
        return bool(reply and reply.get("ok"))
    except OSError:
        return False
    finally:
        sock.close()

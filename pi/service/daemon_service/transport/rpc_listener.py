"""The pi-rpc/1 listener: bind loopback, accept, serve, stop cleanly."""

import socket
import threading

from .rpc_connection import RpcConnection


class RpcListener:
    """It owns the listening socket and its workers, nothing else."""

    def __init__(self, config, codec, access, host="127.0.0.1"):
        self._config = config
        self._codec = codec
        self._access = access
        self._host = host
        self._socket = None
        self._thread = None
        self._connections = set()
        self._workers = []
        self._lock = threading.Lock()
        self._stopping = threading.Event()

    def address(self):
        if self._socket is None:
            return None
        return self._socket.getsockname()

    def start(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((self._host, 0))
        listener.listen(self._config.backlog)
        # A blocking accept does not notice a close from another thread.
        listener.settimeout(self._config.accept_poll)
        self._socket = listener
        self._thread = threading.Thread(target=self._accept,
                                        name="rpc-accept", daemon=True)
        self._thread.start()
        return self

    def _accept(self):
        while not self._stopping.is_set():
            try:
                connection, _ = self._socket.accept()
            except socket.timeout:
                continue
            except (OSError, AttributeError):
                break
            worker = threading.Thread(target=self._serve, args=(connection,),
                                      name="rpc-worker", daemon=True)
            with self._lock:
                self._connections.add(connection)
                self._workers.append(worker)
            worker.start()

    def _serve(self, connection):
        try:
            RpcConnection(connection, self._config, self._codec, self._access,
                          self._stopping).serve()
        finally:
            with self._lock:
                self._connections.discard(connection)
                self._workers = [worker for worker in self._workers
                                 if worker is not threading.current_thread()]

    def stop(self):
        self._stopping.set()
        listener, self._socket = self._socket, None
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(self._config.stop_grace)
        with self._lock:
            connections = list(self._connections)
        # Wake each reader, but keep the write half open: the worker drains
        # its queued replies (a shutdown refusal, a last result) before its
        # writer closes the socket.
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RD)
            except OSError:
                pass
        with self._lock:
            workers = list(self._workers)
        for worker in workers:
            worker.join(self._config.stop_grace)
        with self._lock:
            remaining = list(self._connections)
        for connection in remaining:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                connection.close()
            except OSError:
                pass

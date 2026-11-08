"""One connection's outbound frames, serialized across threads."""

import socket
import threading


class RpcWriter:
    """Notifications and replies leave through here, one frame at a time."""

    def __init__(self, connection, codec):
        self._connection = connection
        self._codec = codec
        self._lock = threading.Lock()
        self._open = True

    def send(self, message):
        frame = self._codec.encode(message)
        with self._lock:
            if not self._open:
                raise OSError("connection closed")
            self._connection.sendall(frame)

    def close(self):
        with self._lock:
            was_open, self._open = self._open, False
        if not was_open:
            return
        try:
            self._connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self._connection.close()
        except OSError:
            pass

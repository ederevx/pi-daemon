"""Bounded JSON-line framing that never loses bytes between frames."""

import socket


class JsonLineStream:
    """One connection's receive buffer and its frame limit."""

    def __init__(self, connection, max_frame_bytes, read_chunk_bytes):
        self._connection = connection
        self._max = max_frame_bytes
        self._chunk = read_chunk_bytes
        self._buffer = b""

    def read(self):
        while True:
            newline = self._buffer.find(b"\n")
            if newline >= 0:
                frame = self._buffer[:newline]
                self._buffer = self._buffer[newline + 1:]
                if len(frame) > self._max:
                    raise ValueError("frame too large")
                return frame
            if len(self._buffer) > self._max:
                raise ValueError("frame too large")
            chunk = self._connection.recv(self._chunk)
            if not chunk:
                if not self._buffer.strip():
                    raise ConnectionError("connection closed")
                frame, self._buffer = self._buffer, b""
                return frame
            self._buffer += chunk

    def write(self, frame):
        self._require()
        self._connection.sendall(frame)

    def release(self):
        """Hand the socket to another owner, with its unread tail."""
        self._require()
        connection, self._connection = self._connection, None
        buffered, self._buffer = self._buffer, b""
        return connection, buffered

    def close(self):
        connection, self._connection = self._connection, None
        if connection is None:
            return
        try:
            connection.close()
        except OSError:
            pass

    def _require(self):
        if self._connection is None:
            raise OSError("the stream no longer owns a connection")
